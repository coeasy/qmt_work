"""V9 Phase 7 DoD：lease reaper —— 租约过期判定死亡，不伪造恢复。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.jobs import JobRuntime, JobSpec  # noqa: E402


def _mk_runtime():
    return JobRuntime()


def _stall_runner(params=None):
    async def _run(job):
        await _sleep_short(job)
        return "never"
    return _run


async def _sleep_short(job):
    import asyncio
    job["report"](1, "stalled")
    await asyncio.sleep(30)


def test_reap_expired_marks_failed():
    rt = _mk_runtime()
    rt.submit(JobSpec(kind="sync", name="x", runner=_stall_runner()))
    job_id = list(rt._jobs.keys())[0]
    job = rt._jobs[job_id]
    job["status"] = "running"
    job["lease_until"] = time.time() - 10.0     # 租约已过期
    reaped = rt.reap_expired()
    assert reaped == [job_id]
    assert rt._jobs[job_id]["status"] == "failed"
    assert "租约" in rt._jobs[job_id]["error"]


def test_healthy_lease_not_reaped():
    rt = _mk_runtime()
    rt.submit(JobSpec(kind="sync", name="x", runner=_stall_runner()))
    job_id = list(rt._jobs.keys())[0]
    job = rt._jobs[job_id]
    job["status"] = "running"
    job["lease_until"] = time.time() + 3600.0   # 租约健康
    assert rt.reap_expired() == []
    assert rt._jobs[job_id]["status"] == "running"


def test_heartbeat_extends_lease():
    rt = _mk_runtime()
    rt.submit(JobSpec(kind="sync", name="x", runner=_stall_runner()))
    job_id = list(rt._jobs.keys())[0]
    job = rt._jobs[job_id]
    job["status"] = "running"
    old_lease = time.time() + 10.0
    job["lease_until"] = old_lease
    job["report"](50, "heartbeat")              # 心跳续租
    assert job["lease_until"] > old_lease


def test_live_task_not_reaped_when_heartbeat_sparse():
    """★ 本进程内任务**还活着**时，心跳稀疏不是「执行者失联」，不得收割。

    实测（2026-09-20 真实库，未连券商）：经典策略选股跑 110s —— 全市场取数
    （5221 只）+ 全池形态识别都是几十秒的纯 CPU/IO 段，中间**没有 report 调用点**
    ⇒ 90s 租约到期 ⇒ 被自己的 reaper 以「lease expired（执行者失联，任务被判死）」
    cancel 掉。后果是**越重的任务越必然失败**（全市场同步、全量回补、EOD 全部中招），
    定时选股链路因此永远出不了结果。

    reaper 的职责是回收**崩溃残留**（上个进程留下的 running 记录），
    不是给慢任务设超时 —— 判据因此是「有没有本地活着的 task」。
    """
    import asyncio

    async def main():
        rt = _mk_runtime()
        rt.submit(JobSpec(kind="sync", name="x", runner=_stall_runner()))
        job_id = list(rt._jobs.keys())[0]
        job = rt._jobs[job_id]
        # 等派发器把它真正跑起来（拿到活着的 task）
        for _ in range(300):
            await asyncio.sleep(0.02)
            t = job.get("task")
            if t is not None and not t.done():
                break
        assert job.get("task") is not None and not job["task"].done(), "任务未被派发"

        job["lease_until"] = time.time() - 5.0      # 制造「心跳稀疏」
        assert rt.reap_expired() == []              # 不许收割活任务
        assert job["status"] == "running"
        assert job["lease_until"] > time.time()     # 已续租

        job["task"].cancel()
        try:
            await job["task"]
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def test_orphan_without_local_task_is_reaped():
    """没有本地执行者的 running 记录（进程崩溃残留）仍然必须被收割。

    这是上面那条修复的**边界**：放宽的只是「有活 task」的情形，
    真正的崩溃残留（``task is None``）照旧判死，否则 running 会永久卡住。
    """
    rt = _mk_runtime()
    rt.submit(JobSpec(kind="sync", name="x", runner=_stall_runner()))
    job_id = list(rt._jobs.keys())[0]
    job = rt._jobs[job_id]
    job["status"] = "running"
    job["task"] = None                              # 崩溃残留：没有本地执行者
    job["lease_until"] = time.time() - 5.0
    assert rt.reap_expired() == [job_id]
    assert job["status"] == "failed"
