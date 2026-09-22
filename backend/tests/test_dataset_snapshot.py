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


def _seed_bars(db, *, batch_id: str = "bars-abc", provider_id: str = "broker"):
    from datasource.local_store import LocalStore

    return LocalStore(db).upsert_bars(
        "600519.SH",
        [{"time": "20260917", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
         {"time": "20260918", "open": 1, "high": 2, "low": 0.5, "close": 1.8}],
        period="1d", adjust="qfq", provider_id=provider_id,
        batch_id=batch_id, schema_version="bars.v2", quality_state="raw")


def test_publish_local_bars_matches_by_batch_id(tmp_path):
    """★ 快照必须按**批次号**回查 ``local_bars``，而不是 (provider_id, batch_id) 组合。

    实测（2026-09-20 真实库）：``local_bars.provider_id`` 写的是**每一行的真实命中
    来源**（broker / tencent / sina…，见 ``app/sync/bars.py::_resolve_provider_id``），
    一次全市场同步里不同标的可能落到不同源；而发布方传的是 ``provider_id='auto'``
    + ``batch_id=<ISO 时间戳>`` —— 两个条件都不成立 ⇒ **永远命中 0 行**。
    于是每份快照的 ``row_count`` 恒为 0、覆盖区间恒为空，却还标着 ``complete``
    （同一份数据的 manifest 里写着 ``bars_written: 623789``，自相矛盾）。
    """
    db = DB(tmp_path / "snap.db")
    assert _seed_bars(db) == 2
    store = DatasetSnapshotStore(db)
    snap = store.publish_local_bars(
        "cn_equity_daily", "v1", "auto", "bars-abc", quality_state="complete")
    assert snap["row_count"] == 2                       # 修复前恒为 0
    assert snap["coverage_start"] == "20260917"
    assert snap["coverage_end"] == "20260918"
    assert store.latest_batch_id() == "bars-abc"
    db._conn.close()


def test_publish_local_bars_ignores_row_level_provider(tmp_path):
    """批次内**每行来源不同**（broker + tencent 混写）时仍应命中全部行。

    「一个 provider_id」不是这批数据的身份 —— 批次号才是。
    """
    db = DB(tmp_path / "snap_mixed.db")
    _seed_bars(db, batch_id="bars-mix", provider_id="broker")
    from datasource.local_store import LocalStore
    LocalStore(db).upsert_bars(
        "000001.SZ",
        [{"time": "20260918", "open": 1, "high": 2, "low": 0.5, "close": 1.9}],
        period="1d", adjust="qfq", provider_id="tencent",
        batch_id="bars-mix", schema_version="bars.v2", quality_state="raw")
    store = DatasetSnapshotStore(db)
    snap = store.publish_local_bars(
        "cn_equity_daily", "v1", "auto", "bars-mix", quality_state="complete")
    assert snap["row_count"] == 3
    db._conn.close()


def test_empty_batch_never_claims_complete(tmp_path):
    """空批次必须标成 ``empty`` 并写明原因，绝不允许伪装成 complete/final。

    实测（2026-09-20 真实库）：批次号传错时回查 0 行，快照照旧写着
    ``quality_state=complete``、``row_count=0`` —— 一份自称「完整」、实际不含
    任何行的数据集元数据；下游研究/回测据此放行等于消费空气。
    """
    import json

    db = DB(tmp_path / "snap_empty.db")
    store = DatasetSnapshotStore(db)
    snap = store.publish_local_bars(
        "cn_equity_daily", "v1", "auto", "no-such-batch", quality_state="complete")
    assert snap["row_count"] == 0
    assert snap["quality_state"] == "empty"             # 不是 complete
    assert "empty_batch" in json.loads(snap["manifest_json"])
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
