"""V9 Phase 7 DoD：misfire 策略（skip / coalesce / catch_up）与相位持久化。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _phase7_support import tmp_db  # noqa: F401,E402
from app.runtime.schedules import MAX_CATCHUP, ScheduleRunner, ScheduleStore  # noqa: E402


class FakeJobRuntime:
    def __init__(self):
        self.submitted = []

    def submit(self, spec):
        self.submitted.append(spec)
        return f"job-{len(self.submitted)}"


def _mk(db, policy):
    store = ScheduleStore(db)
    store.create("system.coverage_report", "30 18 * * 1-5",
                 schedule_id="s1", misfire_policy=policy, enabled=True)
    return store


def test_coalesce_missed_fires_once(tmp_db):
    store = _mk(tmp_db, "coalesce")
    rt = FakeJobRuntime()
    runner = ScheduleRunner(store, rt)
    # 相位停在 9/1（工作日），now=9/10 → 只触发一次
    store.update("s1", next_run_at="2026-09-01T18:30:00")
    submitted = runner.tick_once(now=datetime(2026, 9, 10, 9, 0))
    assert len(submitted) == 1
    assert len(rt.submitted) == 1
    row = store.get("s1")
    assert row["next_run_at"] > "2026-09-10"       # 相位推进到未来
    assert row["last_run_at"] == "2026-09-01T18:30:00"


def test_skip_missed_waits_next_slot(tmp_db):
    store = _mk(tmp_db, "skip")
    rt = FakeJobRuntime()
    runner = ScheduleRunner(store, rt)
    store.update("s1", next_run_at="2026-09-01T18:30:00")
    submitted = runner.tick_once(now=datetime(2026, 9, 10, 9, 0))
    assert len(submitted) == 1
    row = store.get("s1")
    assert row["next_run_at"] >= "2026-09-10"      # 直接等下一相位


def test_catch_up_backfills_capped(tmp_db):
    store = _mk(tmp_db, "catch_up")
    rt = FakeJobRuntime()
    runner = ScheduleRunner(store, rt)
    store.update("s1", next_run_at="2026-08-01T18:30:00")
    submitted = runner.tick_once(now=datetime(2026, 9, 10, 9, 0))
    assert len(submitted) == MAX_CATCHUP           # 跨日 backfill 有上限
    assert len(rt.submitted) == MAX_CATCHUP


def test_not_due_and_disabled(tmp_db):
    store = _mk(tmp_db, "coalesce")
    rt = FakeJobRuntime()
    runner = ScheduleRunner(store, rt)
    store.update("s1", next_run_at="2099-01-01T18:30:00")
    assert runner.tick_once(now=datetime(2026, 9, 10, 9, 0)) == []
    store.update("s1", enabled=False)
    store.update("s1", next_run_at="2026-09-01T18:30:00")
    assert runner.tick_once(now=datetime(2026, 9, 10, 9, 0)) == []


def test_restart_preserves_phase(tmp_db):
    """next_run_at 落库：重建 store/runner 后相位不丢。"""
    store = _mk(tmp_db, "coalesce")
    store.update("s1", next_run_at="2026-09-10T18:30:00")
    # 「重启」：全新实例
    store2 = ScheduleStore(tmp_db)
    row = store2.get("s1")
    assert row["next_run_at"] == "2026-09-10T18:30:00"
    runner = ScheduleRunner(store2, FakeJobRuntime())
    assert runner.tick_once(now=datetime(2026, 9, 10, 9, 0)) == []   # 未到期不触发
    assert runner.tick_once(now=datetime(2026, 9, 10, 19, 0))        # 到期触发
