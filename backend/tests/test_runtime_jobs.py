"""G6 任务运行时测试（配额 / 优先级 / 进度 / 取消）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio

from app.runtime.jobs import GLOBAL_MAX, QUOTA, JobRuntime, JobSpec


async def _wait_status(rt, rid, statuses, timeout=5.0):
    """轮询等待 job 进入目标状态集合。"""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        job = rt.get(rid)
        if job and job["status"] in statuses:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"等待超时：{rid} 状态 {rt.get(rid)}")


async def _wait_all(rt):
    while True:
        jobs = rt.list()
        if jobs and all(j["status"] in ("done", "failed", "canceled") for j in jobs):
            return
        await asyncio.sleep(0.02)


def _sleep_runner(secs, mark=None):
    async def _run(job):
        state = mark if mark is not None else {}
        state["running"] = state.get("running", 0) + 1
        state["peak"] = max(state.get("peak", 0), state["running"])
        try:
            for i in range(10):
                job["report"](i * 10, f"step {i}")
                await asyncio.sleep(secs / 10)
            return {"ok": True}
        finally:
            state["running"] -= 1
    return _run


def test_submit_and_done():
    async def _case():
        rt = JobRuntime()
        rid = rt.submit(JobSpec(kind="screen", name="t", runner=_sleep_runner(0.1)))
        job = await _wait_status(rt, rid, {"done"})
        assert job["status"] == "done"
        assert job["result"] == {"ok": True}
        assert job["progress"] == 100
    asyncio.run(_case())


def test_quota_kind_bounded():
    """kind 配额 1（sync）：4 个 sync 任务并发峰值 ≤ 1。"""
    async def _case():
        rt = JobRuntime()
        state = {}
        for _ in range(4):
            rt.submit(JobSpec(kind="sync", name="s", runner=_sleep_runner(0.15, state)))
        await _wait_all(rt)
        assert state["peak"] == 1, f"sync 并发峰值 {state['peak']} 超配额 1"
        assert QUOTA["sync"] == 1
    asyncio.run(_case())


def test_global_cap():
    """全局上限：不同 kind 各 2 个 → 并发峰值 ≤ GLOBAL_MAX。"""
    async def _case():
        rt = JobRuntime()
        state = {}
        for k in ("backtest", "report"):
            for _ in range(2):
                rt.submit(JobSpec(kind=k, name=k, runner=_sleep_runner(0.15, state)))
        await _wait_all(rt)
        assert state["peak"] <= GLOBAL_MAX
    asyncio.run(_case())


def test_priority_order():
    """优先级：低 priority 值（高优先级）先执行。"""
    async def _case():
        rt = JobRuntime()
        order = []

        def _mark_runner(tag):
            async def _run(job):
                order.append(tag)
                await asyncio.sleep(0.01)
                return tag
            return _run

        rt.submit(JobSpec(kind="screen", name="low", runner=_mark_runner("low"),
                          priority=10))
        rt.submit(JobSpec(kind="screen", name="high", runner=_mark_runner("high"),
                          priority=1))
        await _wait_all(rt)
        assert order == ["high", "low"]
    asyncio.run(_case())


def test_progress_updates():
    async def _case():
        rt = JobRuntime()
        rid = rt.submit(JobSpec(kind="screen", name="t", runner=_sleep_runner(0.2)))
        await _wait_status(rt, rid, {"running"})
        final = await _wait_status(rt, rid, {"done"})
        assert final["progress"] == 100
        assert final["message"] == "完成"
    asyncio.run(_case())


def test_cancel_running():
    async def _case():
        rt = JobRuntime()

        async def _slow(job):
            for _ in range(50):
                await asyncio.sleep(0.02)
            return "never"
        rid = rt.submit(JobSpec(kind="sync", name="s", runner=_slow))
        await _wait_status(rt, rid, {"running"})
        assert await rt.cancel(rid) is True
        final = await _wait_status(rt, rid, {"canceled"})
        assert final["status"] == "canceled"
    asyncio.run(_case())


def test_cancel_queued():
    async def _case():
        rt = JobRuntime()

        async def _slow(job):
            await asyncio.sleep(0.5)
            return 1
        rid1 = rt.submit(JobSpec(kind="sync", name="a", runner=_slow))
        rid2 = rt.submit(JobSpec(kind="sync", name="b", runner=_slow))  # 排队
        await _wait_status(rt, rid1, {"running"})
        assert rt.get(rid2)["status"] == "queued"
        assert await rt.cancel(rid2) is True
        assert rt.get(rid2)["status"] == "canceled"
    asyncio.run(_case())


def test_failed_job_records_error():
    async def _case():
        rt = JobRuntime()

        async def _boom(job):
            raise RuntimeError("模拟失败")
        rid = rt.submit(JobSpec(kind="report", name="r", runner=_boom))
        job = await _wait_status(rt, rid, {"failed"})
        assert "模拟失败" in job["error"]
    asyncio.run(_case())


def test_list_sorted_recent_first():
    async def _case():
        rt = JobRuntime()
        a = rt.submit(JobSpec(kind="screen", name="a", runner=_sleep_runner(0.05)))
        b = rt.submit(JobSpec(kind="screen", name="b", runner=_sleep_runner(0.05)))
        items = rt.list()
        assert items[0]["id"] == b and items[1]["id"] == a
    asyncio.run(_case())
