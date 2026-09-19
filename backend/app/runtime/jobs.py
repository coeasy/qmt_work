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
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.clock import now_iso

log = logging.getLogger("qmt_work.runtime.jobs")

#: kind -> 并发配额；全局并发上限
QUOTA: Dict[str, int] = {"sync": 1, "screen": 1, "backtest": 2, "report": 2}
GLOBAL_MAX = 4

#: kind -> 互斥资源组。**同一组内同时只允许一个 job 运行。**
#:
#: 为什么不能只看 kind 配额：``system.eod`` 内部本就包含日线同步，若与
#: ``system.sync_bars``（或滚动修复 / 对账）并发跑，它们会**同时写 local_bars** ——
#: 互相覆盖、重复计数，且结果取决于谁后写完，完全不可复现。
#: kind 不同 ≠ 不冲突，冲突的是**底层数据资源**，故按资源组互斥。
RESOURCE_GROUP: Dict[str, str] = {
    "system.eod": "local_bars",
    "system.sync_bars": "local_bars",
    "system.rolling_repair": "local_bars",
    "system.reconcile_bars": "local_bars",
    "sync": "local_bars",
}
LEASE_SECONDS = 90.0
#: 进度落库的最小间隔（秒）。见 JobRuntime._make_report 的说明：
#: 高频 report（如 EOD 每完成一只股票一次）若每次都同步写 DB，会把事件循环阻塞住。
_PERSIST_MIN_INTERVAL = 1.0

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

    def __init__(self, db=None, owner: str = ""):
        self._jobs: Dict[str, dict] = {}
        self._queue: List[str] = []          # 排队 job id（按 priority 升序出队）
        self._running: Dict[str, str] = {}   # job_id -> kind
        self._seq = 0
        self._dispatcher: Optional[asyncio.Task] = None
        self._db = db
        self._owner = owner or uuid.uuid4().hex
        #: 任务失败回调（由装配层注入 notifier / 告警引擎）。
        #: 本模块刻意**不直接依赖**告警实现 —— 那样会让运行时与通知耦合，
        #: 且在测试里必须伪造一整套通知栈才能跑。
        self._on_failure: Optional[Callable[[dict], None]] = None

    def set_failure_hook(self, hook) -> None:
        """注册任务失败回调：``hook(job)``，异常被吞掉（通知失败不能拖垮任务）。"""
        self._on_failure = hook

    def _fire_failure(self, job: dict) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure(job)
        except Exception as exc:  # noqa: BLE001 — 通知失败绝不能影响任务本身
            log.warning("任务失败回调异常（已忽略）：%s", exc)

    def _persist(self, job: dict) -> None:
        """把可序列化状态写入 durable ledger；runner/task 不落库。"""
        if self._db is None:
            return
        self._db.upsert("runtime_jobs", {
            "id": job["id"], "kind": job["kind"], "name": job["name"],
            "priority": job["priority"], "status": job["status"],
            "progress": job["progress"], "message": job["message"],
            "created_at": job["created_at"], "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "result_json": json.dumps(job["result"], ensure_ascii=False, default=str),
            "error": job["error"],
            "params_json": json.dumps(job["params"], ensure_ascii=False, default=str),
            "lease_owner": job.get("lease_owner", ""),
            "lease_until": job.get("lease_until"),
            "heartbeat_at": job.get("heartbeat_at"),
            "checkpoint_json": json.dumps(job.get("checkpoint") or {},
                                           ensure_ascii=False, default=str),
        })

    def attach_db(self, db) -> None:
        """挂载持久账本并执行 startup catch-up。"""
        self._db = db
        rows = db.query(
            "SELECT * FROM runtime_jobs WHERE status IN ('queued','running') "
            "ORDER BY priority, created_at")
        for row in rows:
            if row["id"] in self._jobs:
                continue
            try:
                params = json.loads(row.get("params_json") or "{}")
            except (TypeError, ValueError):
                params = {}
            runner_factory = runner_factory_for(row["kind"])
            if runner_factory is None:
                # 未知 runner 不伪造恢复：保留明确失败状态供运维处理。
                db.execute("UPDATE runtime_jobs SET status=?, error=? WHERE id=?",
                           ("failed", "无法恢复未知任务类型", row["id"]))
                continue
            job = {
                "id": row["id"], "seq": self._seq + 1, "kind": row["kind"],
                "name": row["name"], "priority": row["priority"],
                "status": "queued", "progress": row["progress"],
                "message": "启动补跑：重新获取租约", "created_at": row["created_at"],
                "started_at": None, "finished_at": None, "result": None,
                "error": None, "params": params, "task": None,
                "checkpoint": json.loads(row.get("checkpoint_json") or "{}"),
                "lease_owner": "", "lease_until": None, "heartbeat_at": None,
            }
            job["report"] = self._make_report(job)
            job["checkpoint_fn"] = self._make_checkpoint(job)
            job["runner"] = runner_factory(params)
            self._seq += 1
            self._jobs[job["id"]] = job
            self._queue.append(job["id"])
            self._persist(job)

    def _make_report(self, job: dict):
        # 持久化节流（2026-09-14）：调用方可能每完成一个最小单元就 report 一次
        # （EOD 全市场 K 线同步实测 7175 次）。若每次都同步写 DB，事件循环会被
        # 自己的进度回调反复阻塞——py-spy 抓到 MainThread 直接卡在
        # `db.write → upsert(runtime_jobs)`（栈：_tracked → _cb → _report → _persist），
        # 同一时刻读 DB 的 /trade/positions 由 0.019s 恶化到 2.79s。
        # 进度仍实时更新到内存（前端轮询读的就是内存态），只把**落库**节流到
        # >= _PERSIST_MIN_INTERVAL 秒；该值远小于 LEASE_SECONDS(90s)，租约续期
        # 不受影响；终态（done/failed/canceled）由各自分支单独 _persist，
        # 不会被节流吞掉。
        last_persist = [0.0]

        def _report(pct: int, msg: str) -> None:
            job["progress"] = max(0, min(100, int(pct)))
            job["message"] = msg
            job["heartbeat_at"] = time.time()
            # P1-20：心跳续租（report/checkpoint 均视为存活信号）
            if job.get("lease_until"):
                job["lease_until"] = time.time() + LEASE_SECONDS
            now = time.time()
            if now - last_persist[0] < _PERSIST_MIN_INTERVAL:
                return
            last_persist[0] = now
            self._persist(job)
        return _report

    def _make_checkpoint(self, job: dict):
        def _checkpoint(payload: dict) -> None:
            job["checkpoint"] = dict(payload or {})
            job["heartbeat_at"] = time.time()
            if job.get("lease_until"):
                job["lease_until"] = time.time() + LEASE_SECONDS
            self._persist(job)
        return _checkpoint

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

        job["checkpoint"] = {}
        job["lease_owner"] = ""
        job["lease_until"] = None
        job["heartbeat_at"] = None
        job["report"] = self._make_report(job)
        job["checkpoint_fn"] = self._make_checkpoint(job)
        job["runner"] = spec.runner
        self._jobs[job_id] = job
        self._queue.append(job_id)
        self._persist(job)
        self._ensure_dispatcher()
        return job_id

    def get(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        self._ensure_dispatcher()   # 循环内访问时确保派发器存活
        return {k: v for k, v in job.items()
                if k not in ("task", "runner", "report", "checkpoint_fn")}

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
            self._persist(job)
            return True
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
            job["message"] = "取消中…"
            self._persist(job)
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
        """按配额挑下一个可执行 job：kind 配额 + **资源组互斥** + 全局上限，优先级高的先。"""
        running_by_kind: Dict[str, int] = {}
        busy_groups: set[str] = set()
        for kind in self._running.values():
            running_by_kind[kind] = running_by_kind.get(kind, 0) + 1
            grp = RESOURCE_GROUP.get(kind)
            if grp:
                busy_groups.add(grp)
        if len(self._running) >= GLOBAL_MAX:
            return None
        ready = [
            j for j in (self._jobs[i] for i in self._queue)
            if running_by_kind.get(j["kind"], 0) < QUOTA.get(j["kind"], 1)
            # 同组已有 job 在跑 ⇒ 排队等它结束，绝不并发写同一份数据
            and RESOURCE_GROUP.get(j["kind"], "") not in busy_groups
        ]
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
            job["lease_owner"] = self._owner
            job["lease_until"] = time.time() + LEASE_SECONDS
            job["heartbeat_at"] = time.time()
            self._persist(job)
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
            # ★ 失败必须**有人知道**：定时任务最典型的失效模式就是「某天开始
            # 一直失败，但没人看日志」，于是日线不再更新、选股结果一直是旧的，
            # 用户却以为系统在正常跑。日志不是告警 —— 这里把失败抛给外部
            # （notifier / 告警引擎），由装配层决定通知到哪里。
            self._fire_failure(job)
        finally:
            job["finished_at"] = self._now()
            job["lease_owner"] = ""
            job["lease_until"] = None
            self._persist(job)

    # ---------------- P1-20：lease reaper（租约收割） ----------------
    def reap_expired(self, now: Optional[float] = None) -> list[str]:
        """把租约过期仍标记 running 的 job 判定为 failed（进程崩溃残留）。

        不伪造成功：reaper 只能宣告死亡，不能恢复执行；恢复走 attach_db 的
        启动补跑路径（带 checkpoint）。返回被收割的 job id 列表。
        """
        now = time.time() if now is None else now
        reaped: list[str] = []
        for job in self._jobs.values():
            if job["status"] != "running":
                continue
            lease_until = job.get("lease_until")
            if lease_until is None or float(lease_until) > now:
                continue
            job["status"] = "failed"
            job["error"] = "lease expired（租约过期：执行者失联，任务被判死）"
            job["message"] = "租约过期"
            job["finished_at"] = self._now()
            task = job.get("task")
            if task is not None and not task.done():
                task.cancel()
            self._running.pop(job["id"], None)
            self._persist(job)
            reaped.append(job["id"])
        return reaped

    def start_reaper(self, interval: float = 30.0) -> None:
        """启动后台 reaper 循环（幂等）。"""

        async def _loop() -> None:
            while True:
                await asyncio.sleep(max(5.0, float(interval)))
                try:
                    reaped = self.reap_expired()
                    if reaped:
                        log.warning("lease reaper 收割过期任务: %s", reaped)
                except Exception as exc:  # noqa: BLE001
                    log.warning("lease reaper tick failed: %s", exc)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return    # 无运行循环：跳过（测试可手动调 reap_expired）
        if getattr(self, "_reaper_task", None) is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(_loop())

    @staticmethod
    def _now() -> str:
        # V11 R8：旧写法 ``time.strftime("%Y-%m-%dT%H:%M:%S%z")`` 产出 "+0800"
        # （非严格 ISO、且与本文件 :503 的裸值写法**同列不同形**）；统一到 core.clock。
        return now_iso()


_runtime: Optional[JobRuntime] = None
_RUNTIME_FACTORY: dict[str, Callable[[dict], Runner]] = {}
_RLOCK = __import__("threading").Lock()


def get_runtime() -> JobRuntime:
    global _runtime
    if _runtime is None:
        with _RLOCK:
            if _runtime is None:
                _runtime = JobRuntime()
    return _runtime


def register_runner_factory(kind: str, factory: Callable[[dict], Runner]) -> None:
    """注册 kind -> runner 工厂（system.* JobKind 由 app/runtime/system_jobs 注册）。"""
    _RUNTIME_FACTORY[kind] = factory


def runner_factory_for(kind: str) -> Optional[Callable[[dict], Runner]]:
    """按 kind 查 runner 工厂（内置 4 类 + 运行期注册的 system.*）。"""
    return _RUNTIME_FACTORY.get(kind) or {
        "sync": sync_runner, "screen": screen_runner, "backtest": backtest_runner,
    }.get(kind)


# ---------------- 内置 runner：sync / screen ----------------
#: 陈旧比例达到该值即判任务失败（V11 R13）。
#: 实测全市场同步 5224 只里 5095 只陈旧（98.4%）却仍显示 done —— 界面看不出
#: 数据根本没追上。零星几只新鲜说明在线源已被限流/熔断，整体不可用。
STALE_FAIL_RATIO = 0.9


def sync_runner(params: dict) -> Runner:
    """全市场日线同步任务（params: {limit, concurrency, lookback, adjust}）。"""

    async def _run(job: dict) -> dict:
        from app.sync.bars import STALE_DAYS_DEFAULT, BarsSyncer

        def _cb(done: int, total: int, code: str) -> None:
            pct = int(done / total * 100) if total else 100
            job["report"](pct, f"同步 {done}/{total}（{code}）")

        syncer = BarsSyncer(
            concurrency=int(params.get("concurrency") or 8),
            lookback=int(params.get("lookback") or 320),
            provider_id=str(params.get("provider_id") or "auto"),
            batch_id=str(params.get("batch_id") or "") or None,
            # ★ params.adjust 此前**根本没传给同步器**（只用在 snapshot 的
            # adjustment_version 标签上）—— 用户指定「不复权同步」时照跑 qfq。
            # 区别很实际：qfq 链只含真做复权的源（新浪只回不复权，按设计不在
            # 该链内），而 adjust="" 的不复权链含新浪，在线源多一条活路。
            #
            # ⚠️ 不能用 ``params.get("adjust") or "qfq"``：**空字符串是 falsy**，
            # 显式传 ``adjust=""``（不复权）会被静默兜底成 "qfq"，用户的选择丢失
            # 且毫无提示（实测：传 "" 后链里仍无 sina，数据照样追不上）。
            # 只有**未传**（None）才用默认值。
            adjust=("qfq" if params.get("adjust") is None
                    else str(params.get("adjust"))),
            # 新鲜度门槛：最后一根距今超过 N 个自然日即判陈旧并继续降级换源。
            # 传 0/负 = 关闭（恢复「非空即算数」的旧行为）。
            stale_days=int(params.get("stale_days") or STALE_DAYS_DEFAULT),
            # 全量回补（V11 §5.3 P0-3 III）：``mode=full`` 按自然年向前逐页补齐历史，
            # 并基于「本地最早一根」做断点续传。非法值由 BarsSyncer 内部按
            # incremental 处理 —— **绝不静默变全量**（那是几小时的作业）。
            mode=str(params.get("mode") or "incremental"),
            full_years=int(params.get("full_years") or 0) or 12,
        )
        attempts = max(1, int(params.get("max_attempts") or 1))
        summary = None
        for attempt in range(1, attempts + 1):
            summary = await syncer.sync_stock_list(
                limit=int(params.get("limit") or 0) or None, progress_cb=_cb)
            if not summary.failed:
                break
            if attempt < attempts:
                job["report"](0, f"第 {attempt} 次失败，准备重试")
                await asyncio.sleep(min(30.0, 2.0 ** (attempt - 1)))
        result = summary.to_dict() if summary is not None else {}
        # ★ 绝不把「一只都没同步」报成成功。
        # 实测（2026-09-19）：定时任务 status=done / progress=100%，而
        # total=0、bars_written=0、elapsed_ms=0 —— 每天跑、数据一天没更新，
        # 界面还显示「已完成」。静默空转比直接失败危险得多：用户不会去看日志。
        #
        # ⚠️ 例外：全量回补的**断点续传**会让 total=0 成为**合法**结果 ——
        # 第二次跑全量时所有标的的本地历史都已覆盖目标起点，全被跳过。
        # 这不是空转，`skipped_complete` 就是证据。若不排除，重跑全量必报
        # 「股票池为空」的假失败。
        if not result.get("total") and not result.get("skipped_complete"):
            raise RuntimeError(
                "日线同步未获取到任何股票（股票池为空）——"
                "请检查数据源是否可用，或连接券商后重试")
        # ★ 全量模式下「没翻成页」必须让用户看得见（V11 §5.3 P0-3 III）：
        # 此时 ok 是满的、mode 写着 full，但历史**并未**补齐。
        if (str(result.get("mode")) == "full" and result.get("total")
                and not result.get("paged")):
            result["degraded_reason"] = (
                "当前数据源链上没有任何源支持按日期区间取数（免费在线源只接受"
                "最近 N 根），全量回补已退化为单次大窗口 —— 历史未真正补齐，"
                "请连接券商数据源后重跑")
            job["report"](100, "全量回补未生效：当前数据源不支持区间取数")
        # EOD/全市场同步只有在本地批次实际写入后才发布 snapshot；部分失败明确
        # 标成 partial，研究/回测不能把它当成完整数据集使用。
        try:
            from core.db import get_db
            from datasource.snapshots import DatasetSnapshotStore
            quality = ("complete" if not summary.failed and summary.bars_written > 0
                       else "partial" if summary.bars_written > 0 else "empty")
            snap = DatasetSnapshotStore(get_db()).publish_local_bars(
                "cn_equity_daily", str(params.get("batch_id") or summary.finished),
                str(params.get("provider_id") or "auto"),
                str(params.get("batch_id") or summary.finished),
                quality_state=quality,
                calendar_version=str(params.get("calendar_version") or ""),
                adjustment_version=str(params.get("adjust") or "qfq"),
                manifest={"summary": result, "period": "1d",
                          "adjust": params.get("adjust", "qfq")})
            result["dataset_snapshot"] = snap
        except Exception as exc:  # noqa: BLE001
            # Snapshot 发布失败不能伪装成完整同步；保留同步结果并显式告警。
            result["dataset_snapshot_error"] = str(exc)
        # ★★ 绝不把「有数据但全是陈的」报成成功（V11 R13）。
        # 实测（2026-09-18）全市场同步：total=5224 ok=5153 bars=1630066，
        # 界面显示「已完成」—— 但 5093 只的最后一根停在 **20250418**（一年多前），
        # 因为券商本地历史只下载到那天却照样非空，源链「第一个非空即返回」永不降级。
        # 这是继「空转报成功」「对账假空态」之后**第三种假成功**：写了 163 万根
        # 历史数据，选股/回测全在用一年前的行情，而任务状态是 done。
        _ok = int(result.get("ok") or 0)
        _stale = int(result.get("stale") or 0)
        if _stale:
            result["stale_warning"] = (
                f"{_stale}/{_ok} 只标的的数据陈旧（最新 as_of="
                f"{result.get('as_of_max') or '未知'}）")
            job["report"](100, result["stale_warning"])
        if _ok and _stale >= _ok:
            raise RuntimeError(
                f"日线同步写入的 {_ok} 只标的**数据全部陈旧**"
                f"（最新 as_of={result.get('as_of_max') or '未知'}）——"
                f"数据源没有提供近期数据。常见原因：券商客户端本地历史未下载到近期"
                f"（QMT 需手动补下行情）、或未启用在线数据源。")
        # 「几乎全是陈的」同样算失败：实测全市场 5224 只里 5095 只陈旧
        # （98.4%）时任务仍显示 done，界面看不出数据根本没追上。
        # 只有零星几只新鲜 ⇒ 在线源其实已被限流/熔断，整体不可用。
        if _ok and _stale >= _ok * STALE_FAIL_RATIO:
            raise RuntimeError(
                f"日线同步 {_stale}/{_ok} 只标的的数据陈旧"
                f"（{_stale / _ok:.0%}，超过 {STALE_FAIL_RATIO:.0%} 阈值）——"
                f"在线数据源基本不可用（可能已被限流或熔断），只有极少数标的拿到新数据。"
                f"请稍后重试、降低并发，或在 QMT 客户端补齐本地历史行情下载范围。")
        return result

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


def backtest_runner(params: dict) -> Runner:
    """回测任务（params: {symbol, strategy, params, initial_capital, count,
    broker_id, commission_rate, stamp_tax, slippage_bps}）。

    复用 backtest 包的真实引擎（fetch_kline_async_meta + run_backtest_engine），
    依赖主进程内券商连接 → 在事件循环内执行（与 BacktestQueue 同约束）。
    """

    async def _run(job: dict) -> dict:
        from tools.backtest import (
            fetch_kline_async_meta,
            run_backtest_engine,
        )

        symbol = str(params.get("symbol") or "600519.SH")
        strategy = str(params.get("strategy") or "ma_cross")
        pr = params.get("params") or {"fast": 5, "slow": 20}
        capital = float(params.get("initial_capital") or 100_000)
        count = int(params.get("count") or 250)
        broker_id = str(params.get("broker_id") or "")
        cost = {
            "commission_rate": float(params.get("commission_rate") or 0.0003),
            "stamp_tax": float(params.get("stamp_tax") or 0.001),
            "slippage_bps": float(params.get("slippage_bps") or 5.0),
        }
        job["report"](10, "拉取真实历史 K 线")
        kline, meta = await fetch_kline_async_meta(broker_id, symbol, count)
        # V9 §10.4：回测结果挂 Dataset Snapshot 溯源（可复现研究）。
        # 快照缺失/质量不达标不阻断回测（K 线可能来自券商直连而非本地仓），
        # 但来源与质量必须显式随结果落库，不得静默无 provenance。
        dataset_snapshot_id = ""
        dataset_quality = ""
        dataset_warning = ""
        try:
            from core.db import get_db
            from datasource.snapshots import DatasetSnapshotStore, require_quality
            snap = DatasetSnapshotStore(get_db()).latest("cn_equity_daily")
            if snap:
                dataset_snapshot_id = str(snap.get("id", ""))
                dataset_quality = str(snap.get("quality_state", ""))
                try:
                    require_quality(snap)
                except ValueError as exc:
                    dataset_warning = str(exc)
        except Exception:  # noqa: BLE001
            pass
        job["report"](40, "运行回测引擎")
        res = await asyncio.to_thread(
            run_backtest_engine, symbol, kline, strategy, pr, capital,
            cost["commission_rate"], cost["stamp_tax"], cost["slippage_bps"],
            data_meta=meta)
        job["report"](90, "结果落库")
        import json

        from core.db import get_db

        db = get_db()
        bid = db.insert("backtests", {
            "user_id": 1, "symbol": symbol, "start": params.get("start", ""),
            "end": params.get("end", ""), "strategy": strategy,
            "params_json": json.dumps(pr, ensure_ascii=False),
            "initial_capital": capital,
            "metrics_json": json.dumps(res.get("metrics", {}), ensure_ascii=False),
            "trades_json": json.dumps(res.get("trades", []), ensure_ascii=False),
            "dataset_snapshot_id": dataset_snapshot_id,
            "report_path": "", "created_at": now_iso(),
        })
        res["id"] = bid
        res["dataset_snapshot_id"] = dataset_snapshot_id
        if dataset_quality:
            res["dataset_quality"] = dataset_quality
        if dataset_warning:
            res.setdefault("warnings", []).append(dataset_warning)
        job["report"](100, "完成")
        return res

    return _run


__all__ = ["JobRuntime", "JobSpec", "get_runtime",
           "sync_runner", "screen_runner", "backtest_runner",
           "QUOTA", "GLOBAL_MAX", "RESOURCE_GROUP"]
