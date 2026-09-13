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
