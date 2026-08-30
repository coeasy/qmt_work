"""多源行情 DataSourceManager 单元测试（mock 源，无 eltdx / 无网络）。

覆盖：auto 回退、显式源、复权改走补充源、全失败返回 None、熔断、超时、搜索索引。
需在装有 pytest 的环境运行（pytest 未装时可用 `python -m tests.test_datasource` 自查）。
"""
import asyncio

from app.datasource.base import DataSource
from app.datasource.board import classify_board, limit_ratio
from app.datasource.registry import DataSourceManager


class FakeBroker:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def get_quote(self, code):
        if self.fail:
            raise RuntimeError("boom")
        return {"code": code, "last": 1.0}

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        if self.fail:
            raise RuntimeError("boom")
        return [{"close": 1}]

    async def get_instrument_detail(self, code):
        if self.fail:
            raise RuntimeError("boom")
        return {"name": "B", "up_limit_price": 1.1,
                "down_limit_price": 0.9, "pre_close": 1.0}

    async def get_stock_list(self):
        return [{"code": "B.SH", "name": "B"}]


class FakeTDX(DataSource):
    name = "eltdx"

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def get_quote(self, code):
        if self.fail:
            raise RuntimeError("tdx down")
        return {"code": code, "last": 2.0}

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        return [{"close": 2}]

    async def get_instrument_detail(self, code):
        return {"name": "E", "high_limit": 2.2, "low_limit": 1.8,
                "pre_close": 2.0, "industry": "", "concepts": []}

    async def get_stock_list(self):
        return [{"code": "E.SH", "name": "E"}]

    async def search(self, q, limit=20):
        return [{"code": "E.SH", "name": "E"}]


class Slow(DataSource):
    name = "slow"

    async def get_quote(self, code):
        await asyncio.sleep(10)
        return {"code": code, "last": 9}

    async def get_kline(self, *a, **k):
        return []

    async def get_instrument_detail(self, code):
        return {}

    async def get_stock_list(self):
        return []


def _m(broker_fail=False, tdx_fail=False, slow=False):
    m = DataSourceManager()
    m.register_broker(lambda cid: FakeBroker(fail=broker_fail))
    if slow:
        m.register(Slow())
    else:
        m.register(FakeTDX(fail=tdx_fail))
    m.set_auto_chain(["broker", "eltdx"])
    return m


def test_auto_prefers_broker():
    async def c():
        q = await _m().get_quote("X.SH")
        assert q["source"] == "broker" and q["last"] == 1.0
    asyncio.run(c())


def test_auto_falls_back_to_eltdx():
    async def c():
        q = await _m(broker_fail=True).get_quote("X.SH")
        assert q["source"] == "eltdx" and q["last"] == 2.0
    asyncio.run(c())


def test_explicit_source():
    async def c():
        assert (await _m().get_quote("X.SH", source="eltdx"))["source"] == "eltdx"
    asyncio.run(c())


def test_kline_broker_then_adjust_to_eltdx():
    async def c():
        bars, src = await _m().get_kline("X.SH")
        assert src == "broker" and bars[0]["close"] == 1
        bars2, src2 = await _m().get_kline("X.SH", adjust="qfq")
        assert src2 == "eltdx"
    asyncio.run(c())


def test_all_fail_returns_none():
    async def c():
        assert await _m(broker_fail=True, tdx_fail=True).get_quote("X.SH") is None
    asyncio.run(c())


def test_breaker_trips_and_skips():
    async def c():
        m = _m(broker_fail=True)
        for _ in range(3):
            await m.get_quote("X.SH")
        h = await m.health()
        assert "熔断" in h["broker"]["note"]
        q = await m.get_quote("X.SH")
        assert q["source"] == "eltdx"
    asyncio.run(c())


def test_search_stocks_indexed():
    async def c():
        s = await _m().search_stocks("E", 5)
        assert s and s[0]["code"] == "E.SH"
    asyncio.run(c())


def test_slow_source_times_out():
    async def c():
        assert await _m(broker_fail=True, slow=True).get_quote("X.SH") is None
    asyncio.run(c())


def test_classify_and_limit():
    assert classify_board("600519.SH")["board"] == "沪市主板"
    assert classify_board("300750.SZ")["board"] == "创业板"
    assert abs(limit_ratio("600519.SH") - 0.10) < 1e-9
    assert abs(limit_ratio("300750.SZ") - 0.20) < 1e-9
    assert limit_ratio("113050.SH") is None          # 可转债无涨跌幅
    assert abs(limit_ratio("600519.SH", "ST 某某") - 0.05) < 1e-9


if __name__ == "__main__":
    for fn in (test_auto_prefers_broker, test_auto_falls_back_to_eltdx,
               test_explicit_source, test_kline_broker_then_adjust_to_eltdx,
               test_all_fail_returns_none, test_breaker_trips_and_skips,
               test_search_stocks_indexed, test_slow_source_times_out,
               test_classify_and_limit):
        fn()
    print("ALL DATASOURCE TESTS PASSED")
