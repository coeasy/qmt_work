"""V9 Phase 7 DoD：system.* JobKind 注册与可提交。

★ 计数演进：原为 8 个；接入「经典策略选股」定时调度后为 **9** 个
（新增 ``system.classic_screen``，见 app/screener/classic.py）。
本测试锁定的不是「永远是 9」，而是**注册表与 runner 工厂必须一一对应** ——
新增 kind 时同步更新这里，防止出现「能提交但没有 runner」的哑任务。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _phase7_support import tmp_db  # noqa: F401,E402
from app.runtime.jobs import JobRuntime, JobSpec, runner_factory_for  # noqa: E402
from app.runtime.system_jobs import (  # noqa: E402
    SYSTEM_JOB_KINDS,
    register_all,
    runner_for,
)


def test_eight_system_kinds_registered():
    assert len(SYSTEM_JOB_KINDS) == 9
    expected = {
        "system.eod", "system.sync_bars", "system.sync_fundamentals",
        "system.refresh_universe", "system.reconcile_bars",
        "system.rolling_repair", "system.coverage_report",
        "system.publish_snapshot",
        # 经典策略选股（复刻 Sequoia-X），含默认定时调度
        "system.classic_screen",
    }
    assert set(SYSTEM_JOB_KINDS) == expected
    for kind in expected:
        assert runner_for(kind) is not None, kind


def test_register_all_into_job_runtime_factory():
    register_all()
    for kind in SYSTEM_JOB_KINDS:
        assert runner_factory_for(kind) is not None, kind


def test_coverage_runner_executes_end_to_end(tmp_db, monkeypatch):
    """system.coverage_report 真实执行（对临时 DB 出覆盖率报表）。"""
    import app.runtime.system_jobs as sj

    monkeypatch.setattr(sj, "_db", lambda: tmp_db)
    runner = sj._coverage_runner({"lookback_days": 10})
    result = _run(runner({"report": lambda p, m: None}))
    assert "per_day" in result and "provider_share" in result


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_jobsubmit_via_runtime(tmp_db, monkeypatch):
    """system.kind 可经 JobRuntime.submit 提交并跑完（coverage 最轻量）。"""
    import app.runtime.system_jobs as sj

    monkeypatch.setattr(sj, "_db", lambda: tmp_db)
    rt = JobRuntime(db=tmp_db)
    runner = sj._coverage_runner({})
    rid = rt.submit(JobSpec(kind="system.coverage_report",
                            name="cov", runner=runner))
    _wait_done(rt, rid)
    job = rt.get(rid)
    assert job["status"] == "done"
    assert "per_day" in (job["result"] or {})


def _wait_done(rt, rid, timeout=5.0):
    import asyncio

    async def _w():
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            j = rt.get(rid)
            if j and j["status"] in ("done", "failed", "canceled"):
                return j
            await asyncio.sleep(0.05)
        raise AssertionError("job not finished in time")

    return asyncio.run(_w())
