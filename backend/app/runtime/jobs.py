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
    # ★ 定时选股**读**日线，也进组。此前只靠默认调度把两者「错开 15 分钟」，
    #   而 cron 用户可以改 —— 一旦改到同一时刻，选股会读到**半更新**的日线
    #   （部分标的已是今天、部分还是昨天），选出的票无法复现，且因为是无人值守
    #   的定时作业，结果直接落进 screen_runs 没人会复核。时间错开只能当**双保险**，
    #   真正的保证必须落在资源组上。
    "system.classic_screen": "local_bars",
    # ⚠️ 手动选股（kind="screen"）**刻意不进组**：它是用户发起、结果当场可见、
    #   可以立刻重跑；而把它挡在一次 7 分钟的全市场同步后面，用户只会以为卡死了。
}
LEASE_SECONDS = 90.0
#: 进度落库的最小间隔（秒）。见 JobRuntime._make_report 的说明：
#: 高频 report（如 EOD 每完成一只股票一次）若每次都同步写 DB，会把事件循环阻塞住。
_PERSIST_MIN_INTERVAL = 1.0

#: 内存中保留的**已终态**作业上限；``runtime_jobs`` 表保留的终态行上限。
#:
#: 为什么需要：``self._jobs`` 原实现只增不减（``submit`` 与启动补跑各插一条，
#: 终态后永不移除），而 ``result`` 里可能挂着整份 EOD 汇总（几百 KB）；
#: ``GET /runtime/jobs`` 又直接返回全部内存作业 ⇒ 进程内存与响应体都随
#: 任务次数单调上涨。持久账本同理，只 upsert 不删除，库文件持续膨胀。
#: 只淘汰终态：``queued``/``running`` 是调度、取消、进度更新的依据。
_MAX_FINISHED_JOBS = 500
_MAX_PERSISTED_JOBS = 2000
#: 每完成多少个作业裁剪一次持久账本（内存裁剪每次终态都做，仅常数级判断）。
_PERSIST_PRUNE_EVERY = 50

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
        self._reaper_task: Optional[asyncio.Task] = None
        #: 停机后置 True：``get()`` / ``submit()`` 都会经 ``_ensure_dispatcher``
        #: 惰性拉起派发器，停机途中任何一次读接口调用都会把它**复活**，
        #: 于是「已停机」的运行时继续在已关闭的 DB 上写。
        self._stopped = False
        self._db = db
        self._owner = owner or uuid.uuid4().hex
        #: 任务失败回调（由装配层注入 notifier / 告警引擎）。
        #: 本模块刻意**不直接依赖**告警实现 —— 那样会让运行时与通知耦合，
        #: 且在测试里必须伪造一整套通知栈才能跑。
        self._on_failure: Optional[Callable[[dict], None]] = None
        self._finished_since_prune = 0

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

    def attach_db(self, db, *, defer_unknown: bool = False) -> None:
        """挂载持久账本并执行 startup catch-up。

        ``defer_unknown=True``：遇到「此刻还没有 runner 工厂」的 kind 时**先不判定**，
        保持其 ``queued``/``running`` 原状，等工厂注册齐全后再 catch-up 一次。

        ⚠️ 为什么需要这个开关（R25 实测缺陷）：启动顺序是
        ``phase_db``（第一次 ``attach_db``）→ … → ``phase_misc``（``register_all()``
        才注册 ``system.*`` 工厂）。第一次 catch-up 时 ``system.*`` 工厂**尚不存在**，
        于是崩溃前正在执行的 ``system.*`` 任务被直接标成
        ``failed / 无法恢复未知任务类型``；等 phase_misc 再 catch-up 时，这些行
        已不在 ``queued|running`` 里 ⇒ **永不恢复**。
        「现在还不知道」≠「永远不知道」。
        """
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
                if defer_unknown:
                    # 工厂可能稍后才注册 ⇒ 本轮不判定，保持原状态等下一次 catch-up。
                    continue
                # 工厂已注册齐全仍未知 ⇒ 不伪造恢复：保留明确失败状态供运维处理。
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
            self._retention_tick()
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
        if self._stopped:
            return        # 已停机：绝不复活派发器
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
            self._retention_tick()

    # ---------------- 保留策略（防止内存 / durable ledger 无界增长） ----------------
    def _prune_finished(self) -> None:
        """把内存里的已终态作业裁剪到 ``_MAX_FINISHED_JOBS`` 以内（最旧的先淘汰）。

        只淘汰终态作业：``queued`` 还在 ``_queue`` 里等派发，``running`` 还要靠
        内存对象更新进度与响应取消 —— 淘汰它们等于让任务失去控制面。
        被淘汰的作业仍在 ``runtime_jobs`` 表里，按 id 直查历史不受影响。
        """
        terminal = [j for j in self._jobs.values()
                    if j["status"] not in ("queued", "running")]
        excess = len(terminal) - _MAX_FINISHED_JOBS
        if excess <= 0:
            return
        terminal.sort(key=lambda j: j.get("seq", 0))
        for job in terminal[:excess]:
            self._jobs.pop(job["id"], None)
            # 同步移出排队列表：``_next_ready`` 会用 ``self._jobs[i] for i in self._queue``
            # 反查，残留 id 会直接 KeyError。终态作业本不该还在队列里（派发前就出队、
            # 取消排队分支也显式移除），这里是**淘汰动作自身**需要的兜底。
            if job["id"] in self._queue:
                self._queue.remove(job["id"])

    def _prune_persisted(self) -> None:
        """裁剪 ``runtime_jobs`` 表最旧的终态行（表只 upsert、从不删除）。

        保留量（``_MAX_PERSISTED_JOBS``）刻意远大于内存上限：内存淘汰后
        「按 id 直查历史」仍从表读，DB 只做容量保护而不做等量淘汰。
        失败只记日志 —— 清理是运维动作，不能影响任务本身。
        """
        if self._db is None:
            return
        try:
            self._db.execute(
                "DELETE FROM runtime_jobs "
                "WHERE status NOT IN ('queued','running') AND id NOT IN ("
                "  SELECT id FROM runtime_jobs WHERE status NOT IN ('queued','running') "
                "  ORDER BY created_at DESC, id DESC LIMIT ?)",
                (_MAX_PERSISTED_JOBS,))
        except Exception as exc:  # noqa: BLE001
            log.debug("runtime_jobs 裁剪失败（已忽略）：%s", exc)

    def _retention_tick(self) -> None:
        """每个作业进入终态后调用一次：裁剪内存 + （节流）裁剪持久账本。"""
        self._prune_finished()
        self._finished_since_prune += 1
        if self._finished_since_prune % _PERSIST_PRUNE_EVERY == 0:
            self._prune_persisted()

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
            task = job.get("task")
            if task is not None and not task.done():
                # ★★ 本进程内任务**还活着** ⇒ 这不是「执行者失联」，只是**心跳稀疏**。
                #
                # 租约靠 ``report`` / ``checkpoint`` 续期，而真实的长任务会长时间不报进度：
                # 全市场取数（5000+ 只）、全池形态识别都是几十秒到几分钟的纯 CPU/IO 段，
                # 中间没有任何 ``report`` 调用点。此前 reaper 照杀不误 —— 实测
                # （2026-09-20 真实库）经典策略选股跑到 110s 被自己的收割器 cancel，
                # 报「lease expired（执行者失联，任务被判死）」，而它其实一直在正常干活。
                # 后果是**越重的任务越必然失败**：全市场同步、全量回补、EOD 全部中招，
                # 定时选股链路因此永远出不了结果。
                #
                # reaper 的职责是回收**崩溃残留**（上一个进程留下的 running 记录），
                # 不是给慢任务设超时。所以：活任务续租并跳过，只收割真正没有本地执行者
                # 的 job（``task is None`` 或已结束）—— 那种才可能是进程崩溃的残留。
                job["lease_until"] = now + LEASE_SECONDS
                log.info("任务 %s 心跳稀疏（超过 %ss 未报进度）但执行者仍在运行，已续租",
                         job["id"], int(LEASE_SECONDS))
                continue
            job["status"] = "failed"
            job["error"] = "lease expired（租约过期：执行者失联，任务被判死）"
            job["message"] = "租约过期"
            job["finished_at"] = self._now()
            if task is not None and not task.done():
                task.cancel()
            self._running.pop(job["id"], None)
            self._persist(job)
            reaped.append(job["id"])
        return reaped

    def start_reaper(self, interval: float = 30.0) -> None:
        """启动后台 reaper 循环（幂等）。"""
        if self._stopped:
            return        # 已停机：不再拉起新的常驻协程

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

    async def stop(self) -> None:
        """停机：取消派发器、租约收割器与在飞作业（幂等，可重复调用）。

        为什么必须显式停 —— 这里三个东西都是 ``asyncio.create_task`` 出来的
        **常驻**协程，此前**没有任何停机路径**，句柄创建后即被丢弃：

          · ``_dispatcher``（``_dispatch_loop`` 永不退出，空转时 0.05s 一轮）；
          · ``_reaper_task``（``_loop`` 每 30s 收割一次）；
          · 在飞作业任务（``_run`` 可能正写到一半）。

        事件循环关闭时它们被直接销毁（"Task was destroyed but it is pending"），
        而这三者**都会写库** —— 一旦跑在 ``bootstrap.shutdown`` 的 ``db.close()``
        之后，就是「往已关闭的库写」，抛错还可能盖住真正的停机日志。

        取消在飞作业是刻意选择：``_run`` 的 ``CancelledError`` 分支会把作业落成
        ``canceled`` 并**同步**写库（此刻 DB 仍开着），比放任它写已关闭的库安全。
        """
        tasks: List[asyncio.Task] = []
        self._stopped = True
        for attr in ("_dispatcher", "_reaper_task"):
            t = getattr(self, attr, None)
            if t is not None and not t.done():
                tasks.append(t)
            setattr(self, attr, None)
        for job in self._jobs.values():
            if job.get("status") != "running":
                continue
            t = job.get("task")
            if t is not None and not t.done():
                tasks.append(t)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

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
            # ★ 批次号必须用**同步器实际写库用的那个**（``bars-<hex>``）。
            #   此前传 ``summary.finished``（ISO 时间戳）当批次号 ⇒ 快照回查
            #   ``local_bars`` 永远 0 行，于是每份快照都写着 row_count=0 却标 complete。
            snap = DatasetSnapshotStore(get_db()).publish_local_bars(
                "cn_equity_daily",
                str(params.get("batch_id") or summary.batch_id or summary.finished),
                str(params.get("provider_id") or "auto"),
                str(params.get("batch_id") or summary.batch_id or summary.finished),
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
