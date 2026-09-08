"""G1-6 降级策略测试（远程失败 → 本地兜底，stale 明示，降级≠造假）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import pytest

from app.datasource.degrade import envelope, local_bars, local_boards, local_stock_list
from app.datasource.models import Bar, BoardItem, StockInfo
from app.datasource.result import DataResult


@pytest.fixture()
def store(tmp_path):
    from app.datasource.local_store import LocalStore
    from core.db import DB
    db = DB(tmp_path / "test_degrade.db")
    yield LocalStore(db)
    db._conn.close()


def _bars(n=3):
    return [Bar(time=f"2026082{i}", open=10, high=11, low=9, close=10.5 + i,
                volume=1000, amount=10500) for i in range(1, n + 1)]


# ---- K 线兜底 --------------------------------------------------------------
def test_local_bars_fallback(store):
    store.upsert_bars("600519.SH", _bars(3), adjust="qfq")
    dres = local_bars("600519.SH", adjust="qfq", store=store)
    assert isinstance(dres, DataResult)
    assert dres.stale is True
    assert dres.source == "local:sqlite"
    assert dres.as_of == "20260823"          # 真实数据截至时间（最新一根K线日期），非当前时间
    assert len(dres.results) == 3
    assert dres.results[0]["time"] == "20260821"   # 已 model_dump 为 dict
    assert dres.results[0]["close"] == 11.5
    assert dres.warnings and "本地数据仓" in dres.warnings[0]


def test_local_bars_empty_returns_none(store):
    assert local_bars("000001.SZ", store=store) is None


def test_local_bars_adjust_isolated(store):
    """复权维度隔离：库里只有 qfq，按 '' 查不降级（绝不串维度冒充）。"""
    store.upsert_bars("600519.SH", _bars(2), adjust="qfq")
    assert local_bars("600519.SH", adjust="", store=store) is None
    assert local_bars("600519.SH", adjust="qfq", store=store) is not None


# ---- 列表 / 板块兜底 ---------------------------------------------------------
def test_local_stock_list_fallback(store):
    store.upsert_stock_list([StockInfo(code="600519.SH", name="贵州茅台")])
    store.set_meta("last_sync_at", "2026-08-30T12:00:00")
    dres = local_stock_list(store=store)
    assert dres is not None
    assert dres.stale is True and dres.as_of == "2026-08-30T12:00:00"
    assert dres.results[0]["code"] == "600519.SH"


def test_local_stock_list_empty(store):
    assert local_stock_list(store=store) is None


def test_local_boards_fallback(store):
    store.upsert_boards("industry", [BoardItem(code="881001.TI", name="行业A",
                                               change_pct=2.5)])
    dres = local_boards("industry", store=store)
    assert dres is not None and dres.stale is True
    assert dres.results[0]["code"] == "881001.TI"
    assert local_boards("concept", store=store) is None


# ---- 信封合并 --------------------------------------------------------------
def test_envelope_merges_meta():
    dres = DataResult.from_source(
        [{"x": 1}], source="local:sqlite", stale=True, as_of="20260828",
        warnings=["远程行情源不可用，返回本地数据仓数据（截至 20260828）。"])
    data = {"code": "600519.SH", "bars": dres.results, "period": "1d"}
    out = envelope(data, dres)
    assert out["code"] == "600519.SH"        # 原字段保留
    assert out["stale"] is True
    assert out["source"] == "local:sqlite"
    assert out["as_of"] == "20260828"
    assert out["warning"].startswith("远程行情源不可用")
    assert len(out["warnings"]) == 1
