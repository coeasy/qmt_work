"""V9 Phase 7 共享夹具：临时 DB（完整迁移到最新 schema）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from core.clock import local_now  # noqa: E402
from core.db import DB as Database  # noqa: E402


def bar_dt(days_ago: int = 1) -> str:
    """夹具用的 K 线日期（YYYYMMDD）—— **必须相对今天生成**。

    ⚠️ 这里曾经写死 ``"20260910"``，而 ``reconcile_bars(lookback_days=10)`` 的窗口
    下界是「今天 - 10 天」：写死的日期在 **2026-09-21** 那天正好掉出窗口
    （cutoff = 20260911 > 20260910）⇒ 对账统计恒为 0，测试一夜之间从全绿变红，
    而**产品代码一行没动**。

    夹具日期必须永远落在窗口内 ⇒ 按今天倒推 1 天生成。
    """
    from datetime import timedelta
    return (local_now().date() - timedelta(days=int(days_ago))).strftime("%Y%m%d")


#: 本夹具写入的所有行都用这一天（测试查询时也要用它，别再写字面量）
BAR_DT = bar_dt()


@pytest.fixture()
def tmp_db(tmp_path):
    return Database(tmp_path / "test_app.db")


@pytest.fixture()
def eod_db(tmp_db):
    """预置 local_bars 数据（多 provider）供 reconcile/coverage 使用。"""
    bars = [
        # 600000：两源一致（价差 0.1%）
        ("600000.SH", BAR_DT, "broker", 10.0),
        ("600000.SH", BAR_DT, "eltdx", 10.01),
        # 600001：两源冲突（价差 5%）
        ("600001.SZ", BAR_DT, "broker", 20.0),
        ("600001.SZ", BAR_DT, "baostock", 21.0),
        # 600002：单源
        ("600002.SH", BAR_DT, "akshare", 30.0),
    ]
    for code, dt, prov, close in bars:
        tmp_db.execute(
            "INSERT OR REPLACE INTO local_bars "
            "(code, period, adjust, dt, provider_id, close, volume, quality_state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, "1d", "qfq", dt, prov, close, 1000, "raw"))
    tmp_db._conn.commit()
    return tmp_db
