"""universe 多源解析（D-J §J.5）+ 过滤前置（修 P1-32）。

每类 universe 都按链多源降级，失败返回空 codes + degraded + 原因，绝不抛 500 / 绝不伪造。
"""
import asyncio
from app.screener.universe import resolve_universe, UniverseSpec
from _phase4_support import FakeStore


def _run(coro):
    return asyncio.run(coro)


def test_universe_all_uses_local():
    store = FakeStore(stock_list=[{"code": "600000.SH", "name": "浦发"},
                                 {"code": "000001.SZ", "name": "平安"}])
    uni = _run(resolve_universe(UniverseSpec(kind="all"), store=store))
    assert set(uni["codes"]) == {"600000.SH", "000001.SZ"}
    assert uni["provider_used"] == "local"
    assert uni["degraded"] is False


def test_universe_all_falls_back_to_local_bars_when_stock_list_empty():
    """★ ``local_stock_list`` 为空但 ``local_bars`` 有数据时，股票池必须从日线推导。

    为什么必须有这一层：券商适配器**没有 ``get_stock_list`` 能力**（只有
    ``get_sector_stocks``），所以 ``local_stock_list`` 在纯券商环境下**恒为空**
    —— 没有任何东西会去写它。而「本地日线」恰恰是同步任务真正落库的东西。

    实测（2026-09-20 真实库，未连券商）：``local_stock_list`` = 0 行，但
    ``local_bars`` 有 5209 只 / 62 万根日线（截至 20260918）；选股直接回
    「选股股票池为空：local_empty_and_no_broker_sector」——
    **有日线却选不了股，整条选股链路等于没生效**。
    """
    from _phase4_support import make_bar

    store = FakeStore(bars={
        "600519.SH": [make_bar(1500, time_="20260918")],
        "000001.SZ": [make_bar(12, time_="20260918")],
    })
    uni = _run(resolve_universe(UniverseSpec(kind="all"), store=store))
    assert set(uni["codes"]) == {"600519.SH", "000001.SZ"}
    assert uni["provider_used"] == "local_bars"
    assert uni["as_of"] == "20260918"
    # 池子是真的，但名称缺失 ⇒ 必须如实标降级，不能装作完整全市场清单
    assert uni["degraded"] is True
    assert "local_bars" in uni["degraded_reason"]


def test_universe_all_local_bars_window_excludes_delisted():
    """全量回补会把**早已退市**的标的留在 ``local_bars`` 里 ⇒ 必须有时间窗。

    窗口外的标的不得进入当前股票池（否则每天都会对着一批退市票跑形态识别）。
    """
    from _phase4_support import make_bar

    store = FakeStore(bars={
        "600519.SH": [make_bar(1500, time_="20260918")],     # 最近
        "000002.SZ": [make_bar(9, time_="20250418")],        # 一年多前（退市/停更）
    })
    uni = _run(resolve_universe(UniverseSpec(kind="all"), store=store))
    assert uni["codes"] == ["600519.SH"]


def test_universe_all_empty_everywhere_still_degraded():
    """三层兜底全空时仍然是空池 + 明确原因（绝不抛 500、绝不伪造）。"""
    uni = _run(resolve_universe(UniverseSpec(kind="all"), store=FakeStore()))
    assert uni["codes"] == []
    assert uni["degraded"] is True


def test_universe_custom():
    store = FakeStore()
    uni = _run(resolve_universe(UniverseSpec(kind="custom", codes=["600000.SH", "300750.SZ"]),
                                store=store))
    assert uni["codes"] == ["600000.SH", "300750.SZ"]


def test_universe_saved_board():
    store = FakeStore(boards={"screen:my": [{"code": "600000.SH", "name": "浦发"}]})
    uni = _run(resolve_universe(UniverseSpec(kind="saved_board", board="my"), store=store))
    assert uni["codes"] == ["600000.SH"]


def test_universe_saved_board_missing_degraded():
    store = FakeStore()
    uni = _run(resolve_universe(UniverseSpec(kind="saved_board", board="nope"), store=store))
    assert uni["codes"] == []
    assert uni["degraded"] is True


def test_universe_holdings_no_broker_degraded():
    store = FakeStore(stock_list=[{"code": "600000.SH", "name": "浦发"}])
    uni = _run(resolve_universe(UniverseSpec(kind="holdings"), store=store))
    assert uni["codes"] == []
    assert uni["degraded"] is True
    assert "holdings" in (uni["degraded_reason"] or "")


def test_universe_sector_online():
    class Hub:
        async def get_board_constituents(self, code, limit=50, page=0, source="auto"):
            return {"items": [{"code": "600000.SH", "name": "浦发"},
                              {"code": "600036.SH", "name": "招商"}]}, "eltdx"
    uni = _run(resolve_universe(UniverseSpec(kind="sector", value="银行"), hub=Hub()))
    assert set(uni["codes"]) == {"600000.SH", "600036.SH"}
    assert uni["provider_used"] == "eltdx"
