"""V9 Phase 7 共享夹具：临时 DB（完整迁移到最新 schema）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from core.db import DB as Database  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path):
    return Database(tmp_path / "test_app.db")


@pytest.fixture()
def eod_db(tmp_db):
    """预置 local_bars 数据（多 provider）供 reconcile/coverage 使用。"""
    bars = [
        # 600000：两源一致（价差 0.1%）
        ("600000.SH", "20260910", "broker", 10.0),
        ("600000.SH", "20260910", "eltdx", 10.01),
        # 600001：两源冲突（价差 5%）
        ("600001.SZ", "20260910", "broker", 20.0),
        ("600001.SZ", "20260910", "baostock", 21.0),
        # 600002：单源
        ("600002.SH", "20260910", "akshare", 30.0),
    ]
    for code, dt, prov, close in bars:
        tmp_db.execute(
            "INSERT OR REPLACE INTO local_bars "
            "(code, period, adjust, dt, provider_id, close, volume, quality_state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, "1d", "qfq", dt, prov, close, 1000, "raw"))
    tmp_db._conn.commit()
    return tmp_db
