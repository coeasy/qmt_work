"""G1-6 降级策略测试（远程失败 → 本地兜底，stale 明示，降级≠造假）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import pytest

from datasource.degrade import envelope, local_bars, local_boards, local_stock_list
from datasource.models import Bar, BoardItem, StockInfo
from datasource.result import DataResult


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    from datasource.local_store import LocalStore
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


# ---- 路由层：降级路径必须透传 count（P1-1 回归）----------------------------
def _bars_seq(n):
    """生成 n 根时间升序的 K 线（跨月，避免 _bars() 的 9 根上限）。"""
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    return [Bar(time=(base + timedelta(days=i)).strftime("%Y%m%d"),
                open=10, high=11, low=9, close=10.0 + i, volume=1000, amount=10500)
            for i in range(n)]


def test_kline_route_degrade_respects_count(store, monkeypatch):
    """远程无源 → 本地兜底时，返回根数必须 == 请求的 count。

    历史缺陷（P1-1）：`market.py` 调 `local_bars()` 未传 `limit`，而
    `degrade.local_bars` 默认 `limit=500` → 请求 `count=30` 实测返回本地全量
    （320 根），前端图表与指标计算随之失真。回退该修复即 FAILED。
    """
    import asyncio
    from datetime import date, timedelta

    import tools
    import app.routes.market as market_routes

    n_all = 320
    store.upsert_bars("600519.SH", _bars_seq(n_all), adjust="")
    # 路由内 `local_bars` 走 `degrade.get_store()`；把它指到本测试的临时仓。
    monkeypatch.setattr("datasource.degrade.get_store", lambda: store)

    async def _no_remote(*a, **k):
        return {"bars": [], "source": None}          # 模拟远程源彻底不可用
    monkeypatch.setattr(tools, "fetch_kline_cached", _no_remote, raising=False)

    env = asyncio.run(market_routes.market_kline(code="600519.SH", count=30, ctx=None))
    assert env["code"] == 0, env
    data = env["data"]
    assert data["stale"] is True                     # 降级≠造假：显式标 stale
    assert data["source"] == "local:sqlite"
    assert data["count"] == 30                       # ← 核心断言：不是 320
    assert len(data["bars"]) == 30
    # 必须是「最近 30 根」而非最早 30 根（latest-N 语义）
    last = (date(2025, 1, 1) + timedelta(days=n_all - 1)).strftime("%Y%m%d")
    first = (date(2025, 1, 1) + timedelta(days=n_all - 30)).strftime("%Y%m%d")
    assert data["bars"][-1]["time"] == last
    assert data["bars"][0]["time"] == first
