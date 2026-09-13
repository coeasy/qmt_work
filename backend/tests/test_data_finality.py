"""V9 Phase 7 DoD：Data Finality 终态（provisional/final/revised/invalid）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from _phase7_support import tmp_db  # noqa: F401,E402
from datasource.quality import FINALITY_STATES, apply_finality  # noqa: E402


def _mk_snapshot(db, snapshot_id="snap-1", quality="provisional"):
    db.execute(
        "INSERT OR REPLACE INTO dataset_snapshots "
        "(id, dataset_id, version, provider_id, batch_id, as_of, row_count,"
        " quality_state, manifest_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
        (snapshot_id, "cn_equity_daily", "v1", "auto", "b1", "20260910", 0,
         quality, "{}"))
    db._conn.commit()


def test_finality_states_complete_set():
    assert FINALITY_STATES == ("provisional", "final", "revised", "invalid")


def test_apply_finality(tmp_db):
    _mk_snapshot(tmp_db)
    for state in ("final", "revised", "invalid"):
        res = apply_finality(tmp_db, "snap-1", state)
        assert res["finality"] == state
        row = tmp_db.query(
            "SELECT quality_state FROM dataset_snapshots WHERE id='snap-1'")[0]
        assert row["quality_state"] == state


def test_invalid_finality_rejected(tmp_db):
    _mk_snapshot(tmp_db)
    with pytest.raises(ValueError):
        apply_finality(tmp_db, "snap-1", "complete")   # 中间态不是终态
    with pytest.raises(ValueError):
        apply_finality(tmp_db, "snap-1", "whatever")
