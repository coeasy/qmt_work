"""G6 任务运行时与并发闸门（JobRuntime）。

借鉴 Fincept PythonRunner（单例、按类型配额、最大并发），收敛此前散落的并发
设施：sync / screen / backtest / report 四类长任务统一经本运行时调度——
- **配额**：按 kind 限并发（sync=1、screen=1、backtest=2、report=2）+ 全局上限 4；
- **优先级**：priority 越小越先执行（1 最高，5 默认，10 最低）；
- **进度**：job.report(pct, msg) 驱动 status/progress/message 字段（前端轮询
  GET /runtime/jobs/{id} 即可呈现进度，无需 WS）；
- **取消**：job 协程以 asyncio.Task 运行，cancel 即取消任务并标记 canceled。

用法：
    rid = runtime.submit("sync", "全市场同步", sync_job_runner(params))
    runtime.get(rid)  # -> {status, progress, result/error, ...}
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("qmt_work.runtime.jobs")

#: kind -> 并发配额；全局并发上限
QUOTA: Dict[str, int] = {"sync": 1, "screen": 1, "backtest": 2, "report": 2}
GLOBAL_MAX = 4

Runner = Callable[[Dict[str, Any]], Awaitable[Any]]   # async (job) -> result


@dataclass
class JobSpec:
    kind: str
    name: str
    runner: Runner
    priority: int = 5
    params: Optional[dict] = None


class JobRuntime:
    """进程级单例任务运行时（见 get_runtime()）。"""

    def __init__(self):
        self._jobs: Dict[str, dict] = {}
        self._queue: List[str] = []          # 排队 job id（按 priority 升序出队）
        self._running: Dict[str, str] = {}   # job_id -> kind
        self._seq = 0
        self._dispatcher: Optional[asyncio.Task] = None

    # ---------------- 提交与查询 ----------------
    def submit(self, spec: JobSpec) -> str:
        self._seq += 1
        job_id = f"{spec.kind}-{self._seq:04d}"
        job: dict = {
            "id": job_id,
            "seq": self._seq,               # 提交序号（排序/稳定去重用）
            "kind": spec.kind,
            "name": spec.name,
            "priority": spec.priority,
            "status": "queued",
            "progress": 0,
            "message": "排队中",
            "created_at": self._now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "params": spec.params or {},
            "task": None,
        }

        def _report(pct: int, msg: str) -> None:
            job["progress"] = max(0, min(100, int(pct)))
            job["message"] = msg

        job["report"] = _report
        job["runner"] = spec.runner
        self._jobs[job_id] = job
        self._queue.append(job_id)
        self._ensure_dispatcher()
        return job_id

    def get(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        self._ensure_dispatcher()   # 循环内访问时确保派发器存活
        return {k: v for k, v in job.items() if k not in ("task", "runner", "report")}

    def list(self) -> List[dict]:
        jobs = [self.get(j) for j in self._jobs if self.get(j)]
        jobs.sort(key=lambda j: j["seq"], reverse=True)
        return jobs

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job["status"] not in ("queued", "running"):
            return False
        if job["status"] == "queued":
            if job_id in self._queue:
                self._queue.remove(job_id)
            job["status"] = "canceled"
            job["finished_at"] = self._now()
            job["message"] = "已取消（未开始）"
            return True
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
            job["message"] = "取消中…"
            return True
        return False

    # ---------------- 调度 ----------------
    def _ensure_dispatcher(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return    # 无运行循环（submit 可能在循环外调用）：下次循环内访问时再启动
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch_loop())

    def _next_ready(self) -> Optional[dict]:
        """按配额挑下一个可执行 job：kind 配额 + 全局上限，优先级高的先。"""
        running_by_kind: Dict[str, int] = {}
        for kind in self._running.values():
            running_by_kind[kind] = running_by_kind.get(kind, 0) + 1
        if len(self._running) >= GLOBAL_MAX:
            return None
        ready = [j for j in (self._jobs[i] for i in self._queue)
                 if running_by_kind.get(j["kind"], 0) < QUOTA.get(j["kind"], 1)]
        if not ready:
            return None
        ready.sort(key=lambda j: (j["priority"], j["seq"]))
        return ready[0]

    async def _dispatch_loop(self) -> None:
        while True:
            job = self._next_ready()
            if job is None:
                await asyncio.sleep(0.05)
                continue
            self._queue.remove(job["id"])
            self._running[job["id"]] = job["kind"]
            job["status"] = "running"
            job["started_at"] = self._now()
            job["message"] = "执行中"
            task = asyncio.create_task(self._run(job))
            job["task"] = task
            # 不 await：job 任务并行跑，完成后回调清理 _running，循环继续派发
            task.add_done_callback(
                lambda _t, jid=job["id"]: self._running.pop(jid, None))

    async def _run(self, job: dict) -> None:
        try:
            job["result"] = await job["runner"](job)
            job["status"] = "done"
            job["progress"] = 100
            job["message"] = "完成"
        except asyncio.CancelledError:
            job["status"] = "canceled"
            job["message"] = "已取消"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(exc)
            job["message"] = f"失败：{exc}"
            log.warning("任务 %s 失败：%s", job["id"], exc)
        finally:
            job["finished_at"] = self._now()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")


_runtime: Optional[JobRuntime] = None
_RLOCK = __import__("threading").Lock()


def get_runtime() -> JobRuntime:
    global _runtime
    if _runtime is None:
        with _RLOCK:
            if _runtime is None:
                _runtime = JobRuntime()
    return _runtime


# ---------------- 内置 runner：sync / screen ----------------
def sync_runner(params: dict) -> Runner:
    """全市场日线同步任务（params: {limit, concurrency, lookback, adjust}）。"""

    async def _run(job: dict) -> dict:
        from app.sync.bars import BarsSyncer

        def _cb(done: int, total: int, code: str) -> None:
            pct = int(done / total * 100) if total else 100
            job["report"](pct, f"同步 {done}/{total}（{code}）")

        syncer = BarsSyncer(
            concurrency=int(params.get("concurrency") or 8),
            lookback=int(params.get("lookback") or 320),
        )
        summary = await syncer.sync_stock_list(
            limit=int(params.get("limit") or 0) or None, progress_cb=_cb)
        return summary.to_dict()

    return _run


def screen_runner(params: dict) -> Runner:
    """条件选股任务（params: {conditions, limit, sort_by, sort_desc, adjust, max_codes}）。"""

    async def _run(job: dict) -> dict:
        from app.screener.engine import scan

        def _cb(done: int, total: int) -> None:
            job["report"](int(done / total * 100) if total else 100,
                          f"扫描 {done}/{total}")

        return await asyncio.to_thread(
            scan, None, params["conditions"],
            limit=int(params.get("limit") or 100),
            sort_by=params.get("sort_by", "score"),
            sort_desc=bool(params.get("sort_desc", 1)),
            adjust=params.get("adjust", "qfq"),
            max_codes=int(params.get("max_codes") or 0),
            progress_cb=_cb,
        )

    return _run


__all__ = ["JobRuntime", "JobSpec", "get_runtime", "sync_runner", "screen_runner",
           "QUOTA", "GLOBAL_MAX"]
