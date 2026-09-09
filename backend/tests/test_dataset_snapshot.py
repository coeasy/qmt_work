"""Phase 5-7 dataset snapshot, reconcile and calendar persistence tests."""
from datetime import date

import pytest

from app.sync.calendar import CalendarCoverageError, ExchangeCalendarPort
from core.db import DB
from datasource.snapshots import DatasetSnapshotStore, reconcile_bars, require_quality


def test_reconcile_reports_missing_and_mismatch():
    result = reconcile_bars(
        [{"time": "2026-08-27", "close": 10},
         {"time": "2026-08-28", "close": 11}],
        [{"time": "2026-08-27", "close": 10.5}],
    )
    assert result.state == "mismatch"
    assert result.missing == ("2026-08-28",)
    assert result.mismatched == ("2026-08-27",)


def test_snapshot_is_versioned_and_quality_gated(tmp_path):
    db = DB(tmp_path / "snapshot.db")
    store = DatasetSnapshotStore(db)
    snapshot = store.publish(
        "cn_equity_daily", "eod-20260828", "baostock", "eod-20260828",
        [{"dt": "2026-08-28", "code": "600519.SH", "close": 1500}],
        quality_state="complete", manifest={"period": "1d"},
    )
    assert len(snapshot["checksum"]) == 64
    assert store.latest("cn_equity_daily")["version"] == "eod-20260828"
    require_quality(snapshot)
    with pytest.raises(ValueError):
        require_quality({"quality_state": "partial"})
    db._conn.close()


def test_calendar_port_persists_exact_coverage(tmp_path):
    db = DB(tmp_path / "calendar.db")
    port = ExchangeCalendarPort()
    count = port.persist(db, start=date(2026, 8, 24), end=date(2026, 8, 28))
    assert count == 5
    rows = db.query("SELECT * FROM exchange_calendar")
    assert len(rows) == 5
    with pytest.raises(CalendarCoverageError):
        port.persist(db, start=date(2028, 1, 1), end=date(2028, 1, 5))
    db._conn.close()
