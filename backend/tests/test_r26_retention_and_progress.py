"""R26 审计回归：作业进度必须可达 + 作业记录必须有保留上限。

锁死两类**实测缺陷**，两者都不报错、只是静默劣化：

1. **进度恒为 0**：``BacktestQueue._run_compare`` / ``_run_sensitivity`` 读
   ``params["_job_id"]``，而 ``params`` 从路由原样透传、**该键从未被写入** ⇒
   ``self._jobs.get("")`` 恒为 ``None`` ⇒ ``job["progress"]`` 与 ``self._emit``
   是死代码。前端回测页「进度」列于是一直显示 0%，直到终态直接跳 100%。
   修法：把 job 显式作为参数传入，删掉幽灵键。

2. **只增不减**：``BacktestQueue._jobs`` / ``JobRuntime._jobs`` / ``_queue`` 与
   ``runtime_jobs`` / ``backtest_jobs`` 两张表都没有任何上限 —— 终态作业永远留着，
   而 ``job["result"]`` 里可能挂着整份回测明细或 EOD 汇总（几百 KB 级）。
   长期运行 ⇒ 进程内存、``GET /runtime/jobs`` 响应体、库文件三者单调上涨。
   修法：只淘汰**终态**作业（``queued``/``running`` 是调度与取消的依据，必须保留）。

第 26 轮（R26）在此基础上补齐 R1 漏掉的同类缺陷：

3. **常驻后台协程没有停机路径**：``JobRuntime._dispatcher`` / ``_reaper_task``、
   资金流自动采集循环都是 ``create_task`` 出来**永不退出**的协程，句柄创建后被直接
   丢弃。事件循环关闭时它们被销毁（"Task was destroyed but it is pending"），
   而三者都会写库 —— 可能跑在 ``shutdown`` 的 ``db.close()`` 之后。
   修法：``JobRuntime.stop()`` + ``start/stop_moneyflow_collector``，并接入停机顺序。

4. **条件单内存无界增长**：``ConditionOrderEngine._orders`` 只增不减，而
   ``status()``（``GET /trade/conditions``，前端整表渲染）把全部订单原样返回。
   修法：只淘汰终态（``_MAX_TERMINAL_ORDERS``）。

5. **``single_flight`` 窗口缓存无界增长**：``_cache`` 命中后过期也不清理，
   而 key 外部可控（``client_order_id`` / ``idempotency_key``）。
   修法：写入侧按窗口 + 条数上限裁剪。

6. **停机路径「取消但不等待」与 WS 无停机路径**：``SyncEngine.stop()`` 只
   ``cancel()`` 不 await ⇒ ``_batch_loop`` 可能在 ``db.close()`` 后写库；
   ``WSManager`` 完全没有停机路径（心跳任务与连接都只在客户端主动断开时清理）；
   ``WSManager._seq`` 因 cid 永不复用而随重连次数单调上涨。
   修法：``stop()`` 取消即等待；新增 ``WSManager.close()``；``disconnect`` 回收 ``_seq``。

运行：cd backend && python -m pytest tests/test_r26_retention_and_progress.py -q
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest as bq_mod  # noqa: E402
from _phase7_support import tmp_db  # noqa: F401,E402
from app.runtime import jobs as jobs_mod  # noqa: E402


class _FakeDB:
    """不落库的 DB 替身（BacktestQueue 的 _persist 需要）。"""

    def execute(self, *a, **k):
        return None


async def _noop_runner(job):
    return None


# ---------------- 缺陷 1：compare / sensitivity 进度必须真的更新 ----------------
def _patch_engine(monkeypatch):
    """把取数与回测引擎换成纯内存替身，只验证「进度驱动」这一条链路。"""

    async def fake_meta(broker_id, symbol, count):
        return [], {"source": "cache"}

    monkeypatch.setattr(bq_mod, "fetch_kline_async_meta", fake_meta)
    monkeypatch.setattr(bq_mod, "fetch_kline_async", fake_meta)
    monkeypatch.setattr(
        bq_mod, "run_backtest_engine",
        lambda *a, **k: {"metrics": {"sharpe": 1.0, "max_drawdown": -0.1,
                                     "total_return": 0.2},
                         "trades": [], "data_source": "cache"})


def test_compare_progress_reaches_listeners(monkeypatch):
    """compare：每完成一个子任务必须推进度并广播（修复前恒为 0）。"""
    bq_mod.get_db = lambda: _FakeDB()
    _patch_engine(monkeypatch)
    q = bq_mod.BacktestQueue(max_workers=1)
    seen: list[int] = []

    async def on_event(job):
        seen.append(job["progress"])

    async def main():
        q.on_event(on_event)
        configs = [{"symbol": "600519.SH", "params": {"fast": i}} for i in range(4)]
        job = q.create("compare", {"configs": configs})
        await q._run_compare(job["params"], job)
        assert job["progress"] == 100

    asyncio.run(main())
    assert seen == [25, 50, 75, 100], (
        f"compare 进度未按子任务推进（实际 {seen}）—— params['_job_id'] 幽灵键回归？")


def test_sensitivity_progress_reaches_listeners(monkeypatch):
    """sensitivity：同上，逐参数值的进度必须可见。"""
    bq_mod.get_db = lambda: _FakeDB()
    _patch_engine(monkeypatch)
    q = bq_mod.BacktestQueue(max_workers=1)
    seen: list[int] = []

    async def on_event(job):
        seen.append(job["progress"])

    async def main():
        q.on_event(on_event)
        job = q.create("sensitivity", {"values": [3, 5, 10]})
        await q._run_sensitivity(job["params"], job)
        assert job["progress"] == 100

    asyncio.run(main())
    assert seen == [33, 66, 100], f"sensitivity 进度未推进（实际 {seen}）"


def test_compare_without_job_still_works(monkeypatch):
    """无 job 上下文（脚本/单测直调）不得因为进度上报而崩。"""
    bq_mod.get_db = lambda: _FakeDB()
    _patch_engine(monkeypatch)
    q = bq_mod.BacktestQueue()
    res = asyncio.run(q._run_compare({"configs": [{"symbol": "600519.SH"}]}))
    assert res["rows"]


# ---------------- 缺陷 2：作业记录保留上限 ----------------
def test_backtestqueue_prunes_only_terminal(monkeypatch):
    """内存裁剪：只淘汰最旧的终态作业，运行中的必须留下（取消/进度依赖它）。"""
    bq_mod.get_db = lambda: _FakeDB()
    monkeypatch.setattr(bq_mod, "_MAX_JOBS", 3)
    q = bq_mod.BacktestQueue()
    done_ids = []
    for _ in range(5):
        j = q.create("backtest", {})
        j["status"] = "done"
        done_ids.append(j["id"])
    running = q.create("backtest", {})
    running["status"] = "running"

    q._prune_jobs()

    assert len(q._jobs) == 3
    assert running["id"] in q._jobs, "运行中的作业被淘汰 ⇒ cancel/进度会静默失效"
    assert done_ids[0] not in q._jobs and done_ids[1] not in q._jobs
    assert done_ids[-1] in q._jobs, "应保留最新的终态作业"


def test_backtestqueue_close_cancels_inflight():
    """停机：仍在运行的派发协程必须被取消（原 close() 是空实现）。"""
    bq_mod.get_db = lambda: _FakeDB()
    q = bq_mod.BacktestQueue()
    started = asyncio.Event()
    stopped = {"v": False}

    async def long_job():
        try:
            started.set()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped["v"] = True
            raise

    async def main():
        job = q.create("backtest", {})
        q._tasks[job["id"]] = asyncio.create_task(long_job())
        await asyncio.wait_for(started.wait(), timeout=2)
        await q.close()
        for _ in range(50):
            if stopped["v"]:
                break
            await asyncio.sleep(0.02)
        assert stopped["v"] is True, "close() 未取消在飞作业"
        assert q._tasks == {}

    asyncio.run(main())


def test_jobruntime_prunes_terminal_but_keeps_queued(monkeypatch):
    """JobRuntime：淘汰终态、保留排队；且被淘汰的 id 不得残留在 `_queue`。"""
    monkeypatch.setattr(jobs_mod, "_MAX_FINISHED_JOBS", 2)
    rt = jobs_mod.JobRuntime()
    done_ids = []
    for i in range(5):
        jid = rt.submit(jobs_mod.JobSpec(kind="sync", name=f"done{i}",
                                         runner=_noop_runner))
        rt._jobs[jid]["status"] = "done"
        done_ids.append(jid)
    queued_id = rt.submit(jobs_mod.JobSpec(kind="sync", name="queued",
                                           runner=_noop_runner))

    rt._prune_finished()

    terminal = [j for j in rt._jobs.values()
                if j["status"] not in ("queued", "running")]
    assert len(terminal) == 2
    assert queued_id in rt._jobs, "排队中的作业被淘汰 ⇒ 永不派发"
    assert done_ids[-1] in rt._jobs
    assert done_ids[0] not in rt._jobs
    # `_next_ready` 会按 `_queue` 里的 id 反查 `_jobs`，残留 id ⇒ KeyError
    assert all(i in rt._jobs for i in rt._queue)


def test_jobruntime_persisted_ledger_prune(tmp_db, monkeypatch):
    """持久账本：只裁剪最旧的终态行，queued/running 行一行不动。"""
    monkeypatch.setattr(jobs_mod, "_MAX_PERSISTED_JOBS", 3)
    rt = jobs_mod.JobRuntime(db=tmp_db)
    for i in range(6):
        jid = rt.submit(jobs_mod.JobSpec(kind="sync", name=f"j{i}",
                                         runner=_noop_runner))
        rt._jobs[jid]["status"] = "done"
        rt._persist(rt._jobs[jid])
    queued_id = rt.submit(jobs_mod.JobSpec(kind="sync", name="q",
                                           runner=_noop_runner))

    rt._prune_persisted()

    rows = tmp_db.query("SELECT id, status FROM runtime_jobs")
    by_status: dict[str, list[str]] = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r["id"])
    assert len(by_status.get("done", [])) == 3, "终态行未被裁剪到上限"
    assert by_status.get("queued") == [queued_id], "排队中的行被误删 ⇒ 崩溃后无法恢复"


# ---------------- 缺陷 3：常驻后台协程没有停机路径（孤儿任务） ----------------
#
# `JobRuntime._dispatcher` / `_reaper_task` 与资金流采集循环都是
# `asyncio.create_task` 出来的**永不退出**的协程，句柄此前被直接丢弃：
# 事件循环关闭时被销毁（"Task was destroyed but it is pending"），
# 而三者都会写库 —— 一旦跑在 `bootstrap.shutdown` 的 `db.close()` 之后，
# 就是「往已关闭的库写」。


def test_jobruntime_stop_cancels_resident_tasks():
    """stop() 必须取消派发器 + 租约收割器 + 在飞作业，且不残留 pending 任务。"""

    async def main():
        rt = jobs_mod.JobRuntime()
        rt.start_reaper(interval=5.0)
        started = {"v": False}

        async def _slow(job):
            started["v"] = True
            await asyncio.sleep(30)      # 模拟长任务：不取消就永不结束
            return "done"

        rt.submit(jobs_mod.JobSpec(kind="sync", name="slow", runner=_slow))
        for _ in range(100):
            if started["v"]:
                break
            await asyncio.sleep(0.02)
        assert started["v"] is True, "作业未被派发"
        assert rt._dispatcher is not None and not rt._dispatcher.done()
        assert rt._reaper_task is not None and not rt._reaper_task.done()

        await rt.stop()

        assert rt._dispatcher is None and rt._reaper_task is None
        # 在飞作业被落成 canceled，不留 pending task
        running = [j for j in rt._jobs.values() if j["status"] == "running"]
        assert running == [], f"停机后仍有 running 作业：{running}"
        for job in rt._jobs.values():
            t = job.get("task")
            assert t is None or t.done(), "停机后仍有未结束的作业任务"

    asyncio.run(main())


def test_jobruntime_stop_is_idempotent_and_blocks_resurrection():
    """stop() 幂等；停机后任何读接口都不得把派发器复活。"""

    async def main():
        rt = jobs_mod.JobRuntime()
        await rt.stop()
        await rt.stop()                  # 幂等：重复调用不抛

        # `get()` / `submit()` 内部都会惰性拉起派发器，停机后必须被 _stopped 挡住
        jid = rt.submit(jobs_mod.JobSpec(kind="sync", name="after-stop",
                                         runner=_noop_runner))
        rt.get(jid)
        rt.start_reaper(interval=1.0)
        assert rt._dispatcher is None, "停机后派发器被复活 ⇒ 会往已关闭的 DB 写"
        assert rt._reaper_task is None, "停机后租约收割器被复活"

    asyncio.run(main())


def test_moneyflow_collector_stop_is_idempotent():
    """资金流采集：启动句柄必须可停（此前 create_task 返回值被丢弃，无法停）。"""
    from app.services.market import kline_io

    async def main():
        kline_io.start_moneyflow_collector()
        task = kline_io._collector_task
        assert task is not None and not task.done(), "采集任务未启动"
        kline_io.start_moneyflow_collector()      # 幂等：不重复起第二个循环
        assert kline_io._collector_task is task

        await kline_io.stop_moneyflow_collector()
        assert task.done(), "采集任务未被取消"
        assert kline_io._collector_task is None
        await kline_io.stop_moneyflow_collector()  # 幂等：未启动时空操作

    asyncio.run(main())


# ---------------- 缺陷 4：条件单内存无界增长 ----------------
#
# `ConditionOrderEngine._orders` 此前只增不减：`submit` / `load_from_db` 各插一条，
# 进入终态后永不移除，而 `status()`（`GET /trade/conditions`，前端整表渲染）把
# 全部订单原样返回 ⇒ 进程内存与响应体随历史条件单单调上涨。
# 与缺陷 2 同类（只淘汰**终态**）。


def test_condition_engine_prunes_terminal_but_keeps_active(monkeypatch):
    """条件单：终态被裁剪到上限；pending / triggered / submitted 一律保留。"""
    from engines import condition_order as co_mod

    monkeypatch.setattr(co_mod, "_MAX_TERMINAL_ORDERS", 2)
    eng = co_mod.ConditionOrderEngine(manager=None)

    actives = {}
    for st in ("pending", "triggered", "submitted"):
        r = eng.submit("000001.SZ", "buy", "gte", 10.0, 100)
        eng._orders[r["id"]]["status"] = st
        actives[st] = r["id"]

    terminal_ids = []
    for i in range(5):
        r = eng.submit("600000.SH", "sell", "lte", 9.0, 100)
        eng._orders[r["id"]]["status"] = "filled"
        eng._orders[r["id"]]["created_at"] = f"2026-09-2{i}T10:00:00"
        terminal_ids.append(r["id"])

    eng._prune_orders()

    for st, cid in actives.items():
        assert cid in eng._orders, f"{st} 条件单被误裁 ⇒ 订单失控（无法监控/核销/取消）"
    terminal = [o for o in eng._orders.values()
                if o["status"] not in co_mod._ACTIVE_STATUSES]
    assert len(terminal) == 2, "终态条件单未被裁剪到上限"
    # 最旧的先淘汰
    assert terminal_ids[0] not in eng._orders
    assert terminal_ids[-1] in eng._orders
    # 淘汰不得留下「重试队列指向已不存在订单」的悬垂引用
    assert all(i in eng._orders for i in eng._retry_queue)


def test_condition_engine_prune_removes_retry_queue_dangling_ref(monkeypatch):
    """终态被淘汰时，重试队列里的同名条目必须一起清掉（否则每轮都查一个幽灵订单）。"""
    from engines import condition_order as co_mod

    monkeypatch.setattr(co_mod, "_MAX_TERMINAL_ORDERS", 0)
    eng = co_mod.ConditionOrderEngine(manager=None)
    cid = eng.submit("000001.SZ", "buy", "gte", 10.0, 100)["id"]
    eng._orders[cid]["status"] = "failed"
    eng._retry_queue[cid] = eng._orders[cid]      # 人为构造悬垂引用

    eng._prune_orders()

    assert cid not in eng._orders
    assert cid not in eng._retry_queue, "悬垂引用未清理"


# ---------------- 缺陷 5：single_flight 窗口缓存无界增长 ----------------


def test_single_flight_cache_is_bounded():
    """★ R26：`_cache` 必须按窗口裁剪（key 外部可控，随机 key 可无限撑内存）。"""
    from gateway import idempotency as idem

    async def run_one(k):
        async def _do():
            return {"ok": True}
        return await idem.single_flight(k, _do)

    async def main():
        original = (idem._MAX_CACHE, idem._PRUNE_EVERY)
        idem._MAX_CACHE, idem._PRUNE_EVERY = 10, 1     # 收紧以便观测
        try:
            idem._cache.clear()
            for i in range(200):
                await run_one(f"k{i}")
            assert len(idem._cache) <= 10, f"窗口缓存无上限：{len(idem._cache)}"
        finally:
            idem._cache.clear()
            idem._MAX_CACHE, idem._PRUNE_EVERY = original

    asyncio.run(main())


def test_single_flight_still_dedupes_within_window():
    """裁剪不得破坏幂等语义：窗口内同 key 仍复用结果、只执行一次。"""
    from gateway import idempotency as idem

    async def main():
        idem._cache.clear()
        calls = {"n": 0}

        async def _do():
            calls["n"] += 1
            return {"ok": True, "seq": calls["n"]}

        first = await idem.single_flight("dup-key", _do)
        second = await idem.single_flight("dup-key", _do)
        assert calls["n"] == 1, "同 key 在窗口内被执行了两次 ⇒ 重复下单"
        assert second.get("duplicated") is True, second
        assert first["seq"] == second["seq"]
        idem._cache.clear()

    asyncio.run(main())


# ---------------- 缺陷 6：停机路径「取消但不等待」+ WS 无停机路径 ----------------
#
# `SyncEngine.stop()` 原来只 `cancel()` 不 await：取消是**异步**的，`_batch_loop`
# （每 100ms 批量写 market_cache）可能在 `db.close()` 之后才跑到写入那一行。
# `WSManager` 更是**完全没有停机路径**：`_hb_tasks` / `_sockets` 只在客户端主动
# 断开时清理，正常停机时心跳协程被循环销毁、连接收不到关闭帧。


def test_sync_engine_stop_awaits_batch_loop():
    """stop() 必须等循环真正退出（句柄置空），而不是只发一个 cancel。"""
    from sync import SyncEngine

    class _Mgr:
        def all_connections(self):
            return []

    async def main():
        eng = SyncEngine(_Mgr(), db=None)
        eng.start_batch()
        task = eng._batch_task
        assert task is not None and not task.done()
        await asyncio.sleep(0.15)                 # 至少跑过一轮 100ms 窗口

        await eng.stop()
        assert task.done(), "stop() 未等待微批 flush 循环退出 ⇒ 可能在 db.close() 后写库"
        assert eng._batch_task is None
        await eng.stop()                          # 幂等

    asyncio.run(main())


def test_ws_manager_close_cancels_heartbeats_and_sockets():
    """close() 必须取消全部心跳任务、关闭全部连接（此前完全没有停机路径）。"""
    from sync import WSManager

    class _Engine:
        _client_subscriptions: dict = {}
        latest_quotes: dict = {}

        def client_unsubscribe(self, cid, codes):    # noqa: D102
            pass

    class _WS:
        def __init__(self):
            self.closed = False

        async def accept(self):
            return None

        async def send_text(self, _s):
            return None

        async def close(self):
            self.closed = True

    async def main():
        mgr = WSManager(_Engine())
        socks = [_WS(), _WS()]
        for ws in socks:
            await mgr.connect(ws)
        hb = list(mgr._hb_tasks.values())
        assert len(hb) == 2 and all(not t.done() for t in hb)

        await mgr.close()

        assert mgr._hb_tasks == {} and mgr._sockets == {} and mgr._seq == {}
        assert all(ws.closed for ws in socks), "停机未向客户端发送关闭帧"
        await asyncio.sleep(0)
        assert all(t.done() for t in hb), "心跳任务未结束（会在循环关闭时被销毁）"
        await mgr.close()                        # 幂等

    asyncio.run(main())


def test_ws_manager_disconnect_reclaims_seq():
    """cid 永不复用 ⇒ `_seq` 必须在 disconnect 时回收，否则随重连次数单调上涨。"""
    from sync import WSManager

    class _Engine:
        _client_subscriptions: dict = {}
        latest_quotes: dict = {}

        def client_unsubscribe(self, cid, codes):    # noqa: D102
            pass

    class _WS:
        async def accept(self):
            return None

        async def send_text(self, _s):
            return None

    async def main():
        mgr = WSManager(_Engine())
        cid = await mgr.connect(_WS())
        assert cid in mgr._seq
        mgr.disconnect(cid)
        assert cid not in mgr._seq, "_seq 未回收 ⇒ 连接次数越多内存越大"
        assert cid not in mgr._sockets and cid not in mgr._hb_tasks

    asyncio.run(main())