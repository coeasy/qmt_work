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
