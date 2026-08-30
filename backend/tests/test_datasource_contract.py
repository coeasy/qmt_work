"""G1 统一数据契约测试（标准模型 + DataResult + DataSource 模型访问器）。

验证「换源字段一致」「缺失即 None（零 mock）」「降级必须带 as_of」三条铁律。
注意：后端测试须逐文件运行（同进程全量会硬崩溃），不要整包 pytest。
"""
import asyncio

import pytest

from app.datasource.base import DataSource
from app.datasource.models import (
    Bar,
    BoardItem,
    InstrumentInfo,
    Moneyflow,
    Quote,
    StockInfo,
)
from app.datasource.result import DataResult


# ---- 1. 标准模型：对齐 eltdx 真实返回（camelCase） -------------------------
def test_quote_from_eltdx_shape():
    raw = {
        "code": "600519.SH",
        "name": "贵州茅台",
        "last": 1680.0,
        "open": 1670.0,
        "high": 1690.0,
        "low": 1660.0,
        "lastClose": 1666.0,
        "volume": 3210456,
        "amount": 5.38e9,
        "bid": 1679.0,
        "ask": 1680.0,
        "bids": [{"price": 1679.0, "volume": 1200}],
        "asks": [{"price": 1680.0, "volume": 900}],
        "change": 14.0,
        "change_pct": 0.84,
        "inside": 123456,
        "outside": 234567,
        "ts": "2026-08-30T15:00:00",
    }
    q = Quote.model_validate(raw)
    assert q.code == "600519.SH"
    assert q.last == 1680.0
    assert q.last_close == 1666.0          # camelCase lastClose 映射到 snake
    assert q.bids[0].price == 1679.0
    assert q.bids[0].volume == 1200
    assert q.inside == 123456


def test_quote_missing_fields_are_none():
    """缺失字段一律 None，绝不估算填充（零 mock 铁律）。"""
    q = Quote.model_validate({"code": "000001.SZ"})
    assert q.code == "000001.SZ"
    assert q.last is None
    assert q.change_pct is None
    assert q.bids is None


def test_quote_snake_construct():
    """populate_by_name：允许 snake_case 直接构造。"""
    q = Quote(code="600519.SH", last=1.0, last_close=0.9)
    assert q.last == 1.0
    assert q.last_close == 0.9


def test_bar_from_raw():
    raw = {"time": "20260829", "open": 10, "high": 11, "low": 9, "close": 10.5,
           "volume": 1000, "amount": 10500}
    b = Bar.model_validate(raw)
    assert b.time == "20260829"
    assert b.close == 10.5
    assert b.volume == 1000


def test_instrument_model():
    # 真实源不返回 code（契约仅 name/exchange/limits/pre_close），故 code 可选
    raw = {"name": "贵州茅台", "exchange": "SH", "high_limit": 1832.6,
           "low_limit": 1499.4, "pre_close": 1666.0}
    m = InstrumentInfo.model_validate(raw)
    assert m.code is None
    assert m.high_limit == 1832.6
    assert m.pre_close == 1666.0


def test_stock_list_model():
    raw = [{"code": "600519.SH", "name": "贵州茅台", "category": "主板"}]
    items = [StockInfo.model_validate(s) for s in raw]
    assert items[0].category == "主板"


def test_board_item_model():
    raw = {"code": "881001.TI", "name": "通达信行业", "kind": "industry",
           "last": 1200.5, "change_pct": 1.23, "amount": 9.9e8}
    b = BoardItem.model_validate(raw)
    assert b.kind == "industry"
    assert b.change_pct == 1.23


def test_moneyflow_model():
    raw = {"code": "600519.SH", "inside": 100, "outside": 200, "net": -100,
           "strength": [{"t": "09:30", "buy": 10, "sell": 5}],
           "volume_ratio": 1.5, "ts": "2026-08-30T15:00:00"}
    m = Moneyflow.model_validate(raw)
    assert m.net == -100
    assert m.strength[0].buy == 10
    assert m.volume_ratio == 1.5


# ---- 2. DataResult 容器契约 ----------------------------------------------
def test_dataresult_fresh_to_dict():
    r = DataResult.from_source([1, 2, 3], source="eltdx")
    d = r.to_dict()
    assert d["source"] == "eltdx"
    assert d["stale"] is False
    assert d["as_of"] is None
    assert d["results"] == [1, 2, 3]


def test_dataresult_stale_requires_as_of():
    """降级必须带 as_of，否则视为「静默造假」直接拒绝。"""
    with pytest.raises(ValueError):
        DataResult(results=[], source="local:sqlite", stale=True)  # 缺 as_of
    ok = DataResult(results=[], source="local:sqlite", stale=True,
                    as_of="2026-08-29T15:00:00")
    assert ok.stale is True
    assert ok.as_of == "2026-08-29T15:00:00"


def test_dataresult_empty_source_rejected():
    with pytest.raises(ValueError):
        DataResult(results=[], source="")


def test_dataresult_extra():
    r = DataResult.from_source({"x": 1}, source="broker", upstream_ms=12)
    assert r.to_dict()["extra"] == {"upstream_ms": 12}


# ---- 3. DataSource 模型访问器（向后兼容，eltdx 无需改动） ------------------
class _StubSource(DataSource):
    name = "stub"

    async def get_quote(self, code: str) -> dict:
        return {"code": code, "last": 10.0, "lastClose": 9.0, "change": 1.0,
                "change_pct": 11.1, "bids": [{"price": 9.9, "volume": 100}]}

    async def get_kline(self, code: str, period="1d", count=250, adjust=None) -> list:
        return [{"time": "20260829", "open": 9, "high": 10, "low": 8, "close": 9.5,
                 "volume": 1000, "amount": 9500}]

    async def get_instrument_detail(self, code: str) -> dict:
        return {"name": "测试", "exchange": "SH", "high_limit": 11.0,
                "low_limit": 9.0, "pre_close": 9.0}

    async def get_stock_list(self) -> list:
        return [{"code": "600519.SH", "name": "贵州茅台", "category": "主板"}]


def test_datasource_get_quote_model():
    src = _StubSource()
    q = asyncio.run(src.get_quote_model("600519.SH"))
    assert isinstance(q, Quote)
    assert q.last == 10.0
    assert q.last_close == 9.0
    assert q.bids[0].price == 9.9


def test_datasource_get_kline_models():
    src = _StubSource()
    bars = asyncio.run(src.get_kline_models("600519.SH"))
    assert len(bars) == 1
    assert isinstance(bars[0], Bar)
    assert bars[0].close == 9.5


def test_datasource_get_instrument_model():
    src = _StubSource()
    m = asyncio.run(src.get_instrument_model("600519.SH"))
    assert isinstance(m, InstrumentInfo)
    assert m.code == "600519.SH"       # 源无 code，访问器注入
    assert m.high_limit == 11.0


def test_datasource_get_stock_list_models():
    src = _StubSource()
    items = asyncio.run(src.get_stock_list_models())
    assert isinstance(items[0], StockInfo)
    assert items[0].category == "主板"


def test_datasource_model_none_safe():
    """源返回 None 时模型访问器返回 None/[]，不以空模型冒充。"""

    class _Empty(DataSource):
        name = "empty"

        async def get_quote(self, code: str):
            return None

        async def get_kline(self, code, period="1d", count=250, adjust=None):
            return None

        async def get_instrument_detail(self, code: str):
            return None

        async def get_stock_list(self):
            return None

    src = _Empty()
    assert asyncio.run(src.get_quote_model("X")) is None
    assert asyncio.run(src.get_kline_models("X")) == []
    assert asyncio.run(src.get_instrument_model("X")) is None
    assert asyncio.run(src.get_stock_list_models()) == []
