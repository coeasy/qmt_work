"""V9 Phase 7 DoD：跨源对账 reconcile_bars。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _phase7_support import eod_db, tmp_db  # noqa: F401,E402
from datasource.quality import PROVIDER_QUALITY_RANK, reconcile_bars  # noqa: E402


def test_consistent_sources_canonical_final(eod_db):
    stats = reconcile_bars(eod_db, lookback_days=10)
    assert stats["groups"] >= 2
    # 一致组：最优源（broker 排最前）标 final
    rows = eod_db.query(
        "SELECT provider_id, quality_state FROM local_bars "
        "WHERE code='600000.SH' AND dt='20260910'")
    by_prov = {r["provider_id"]: r["quality_state"] for r in rows}
    assert by_prov["broker"] == "final"
    assert by_prov["eltdx"] in ("raw", "reconciled", "final")   # raw 保留
    assert stats["conflicts"] >= 1


def test_conflict_keeps_all_raw_marked(eod_db):
    stats = reconcile_bars(eod_db, lookback_days=10)
    assert stats["conflicts"] == 1
    rows = eod_db.query(
        "SELECT provider_id, quality_state FROM local_bars "
        "WHERE code='600001.SZ' AND dt='20260910'")
    assert len(rows) == 2                          # 全部 raw 保留，不删行
    assert all(r["quality_state"] == "conflict" for r in rows)


def test_single_source_untouched(eod_db):
    reconcile_bars(eod_db, lookback_days=10)
    rows = eod_db.query(
        "SELECT quality_state FROM local_bars WHERE code='600002.SH'")
    assert rows[0]["quality_state"] == "raw"       # 单源组不参与对账


def test_provider_quality_rank_contract():
    assert PROVIDER_QUALITY_RANK["broker"] < PROVIDER_QUALITY_RANK["eltdx"]
    assert PROVIDER_QUALITY_RANK["eltdx"] < PROVIDER_QUALITY_RANK["baostock"]
    assert PROVIDER_QUALITY_RANK["baostock"] < PROVIDER_QUALITY_RANK["akshare"]
