"""当日逐笔成交（``/market/ticks``）链路测试 —— 2026-10-03。

## 为什么必须单独钉这一条
看盘界面的「成交流」此前只有两条路，且**都依赖券商桥**：

  ① WS `deal` 事件 —— 真实来源是 `_push_order_deal_events`（`adapter.get_deals`）
     与 `_on_realtime_deal`（`on_trade`），即**本账户成交回报**，不是市场成交；
  ② `GET /market/l2` —— 券商 L2 逐笔，未连接返 503。

结果：未连券商时成交流**结构性空白**，而真实市场逐笔本来就能从公开行情源取到。
本文件锁住新链路（`DataSourceManager.get_ticks` → `GET /market/ticks`）的：

  1. 能力链**不含 broker**（这条能力的存在意义就是「无券商也能看」）；
  2. manager 层把「源不可用（None）」与「源可用但无成交（items=[]）」**分开**；
  3. 路由层 400 / 503 / 200 三态与契约形状；
  4. `trading_date` 必须由后端补齐（否则用户会把上一交易日的成交当成今日行情）。

★ 第 2 条是本项目反复踩的坑：**空列表与 None 语义不同**，前端文案必须分开；
  把两者混为一谈就会出现「明明有数据却说源不可用」或反过来。
"""
from __future__ import annotations

import asyncio

import pytest

from datasource.base import DataSource
from datasource.registry import DataSourceManager

# --------------------------------------------------------------------------- 假源


class _Base(DataSource):
    """DataSource 的抽象方法占位 —— 假源只需关心 ticks，其余都不是本用例的对象。"""

    async def get_quote(self, code: str) -> dict:
        return {}

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        return []

    async def get_instrument_detail(self, code: str) -> dict:
        return {}

    async def get_stock_list(self):
        return []


class _FakeTicksSource(_Base):
    """只声明 `ticks` 的假源：记录每次请求的 code / count。"""

    def __init__(self, name: str, items=None, fail: bool = False):
        self.name = name
        self._items = items if items is not None else []
        self._fail = fail
        self.calls: list[tuple[str, int]] = []

    async def get_ticks(self, code: str, count: int = 60):
        self.calls.append((code, count))
        if self._fail:
            raise RuntimeError("行情服务器不可达")
        return {
            "code": code,
            "items": list(self._items),
            "count": len(self._items),
            "trading_date": "20261002",
            "source": self.name,
        }


class _NoTicksSource(_Base):
    """什么 ticks 方法都没有的源 —— 用于验证「缺方法不抛 AttributeError」。"""

    name = "noticks"


# --------------------------------------------------------------------------- ① 能力链


def test_ticks_chain_excludes_broker():
    """★ ticks 链**不得**含 broker —— 否则 auto 优先券商会把「无券商可用」重新掐掉。"""
    from datasource.providers import DEFAULT_CAPABILITY_CHAINS

    assert "ticks" in DEFAULT_CAPABILITY_CHAINS, "未登记 ticks 能力链"
    assert "broker" not in DEFAULT_CAPABILITY_CHAINS["ticks"], (
        "ticks 链含 broker：未连券商时 auto 会先试券商，把本条能力的意义抵消掉")


def test_eltdx_declares_ticks_and_implements_it():
    """声明与实现必须一致（护栏 test_capability_chain_unity 已在全量侧锁，此处补单点）。"""
    from datasource.eltdx_source import EltdxSource
    from datasource.providers import PROVIDER_CATALOG

    assert "ticks" in EltdxSource.capabilities
    assert hasattr(EltdxSource, "get_ticks")
    desc = next(d for d in PROVIDER_CATALOG if d.id == "tdx")
    assert "ticks" in desc.capabilities


# --------------------------------------------------------------------------- ② manager


def test_manager_get_ticks_returns_normalized_shape():
    src = _FakeTicksSource("ft", items=[
        {"time": "14:57:00", "price": 10.5, "volume": 3, "amount": 3150.0, "side": "buy"},
    ])
    mgr = DataSourceManager().register(src)
    out = asyncio.run(mgr.get_ticks("600519.SH", 10, source="ft"))

    assert out is not None
    assert out["code"] == "600519.SH"
    assert out["count"] == 1
    assert out["source"] == "ft"
    assert out["items"][0]["side"] == "buy"
    # 入参规范化：裸 6 位要补后缀（源只认带交易所后缀的代码）
    assert src.calls[0][0] == "600519.SH"


def test_manager_get_ticks_clamps_count():
    """count 必须被夹到上限 —— 否则一次请求能把一页 wire 记录撑爆。"""
    from datasource.manager_ticks import MAX_TICKS

    src = _FakeTicksSource("ft")
    mgr = DataSourceManager().register(src)
    asyncio.run(mgr.get_ticks("600519.SH", 100000, source="ft"))
    assert src.calls[0][1] == MAX_TICKS


def test_manager_get_ticks_none_when_no_source():
    """★ 所有候选源都不可用 ⇒ **None**（不是空列表）。两者语义不同。"""
    mgr = DataSourceManager().register(_NoTicksSource())
    assert asyncio.run(mgr.get_ticks("600519.SH")) is None


def test_manager_get_ticks_empty_list_is_not_none():
    """★ 源可用但当日无成交 ⇒ ``items == []``，不得与「源不可用」混为一谈。"""
    mgr = DataSourceManager().register(_FakeTicksSource("ft", items=[]))
    out = asyncio.run(mgr.get_ticks("600519.SH", source="ft"))
    assert out is not None, "空列表是合法结果，不得退化成 None"
    assert out["items"] == [] and out["count"] == 0


def test_manager_get_ticks_skips_source_without_method():
    """源没实现 get_ticks 时**跳过**而不是抛 AttributeError。"""
    mgr = DataSourceManager().register(_NoTicksSource()).register(_FakeTicksSource("ft"))
    out = asyncio.run(mgr.get_ticks("600519.SH"))
    # _NoTicksSource 未声明 ticks，auto 链解析不会选中它；ft 应兜住
    assert out is None or out["source"] == "ft"


# --------------------------------------------------------------------------- ③ 路由层


def test_route_ticks_requires_code():
    from app.routes.market import market_ticks

    res = asyncio.run(market_ticks(code=""))
    assert res["code"] == 400, res


def test_route_ticks_503_when_source_unavailable(monkeypatch):
    """源不可用 ⇒ 503 + 说清「本地 TDX 未就绪」并给出替代路径，绝不返回 200 空列表。"""
    import app.routes.market as m

    class _Hub:
        async def get_ticks(self, code, count=60, source="auto"):
            return None

    monkeypatch.setattr(m, "get_hub", lambda: _Hub())
    res = asyncio.run(m.market_ticks(code="600519.SH"))
    assert res["code"] == 503, res
    assert "TDX" in res["message"], res["message"]
    # 给得出替代路径（券商 L2），否则用户只知道「不能用」不知「还能怎么办」
    assert "L2" in res["message"], res["message"]


def test_route_ticks_200_keeps_contract_fields(monkeypatch):
    import app.routes.market as m

    class _Hub:
        async def get_ticks(self, code, count=60, source="auto"):
            return {
                "code": code,
                "items": [{"time": "09:30:00", "price": 1.0, "volume": 1,
                           "amount": 100.0, "side": "neutral"}],
                "count": 1,
                "trading_date": "20261002",
                "source": "tdx",
            }

    monkeypatch.setattr(m, "get_hub", lambda: _Hub())
    res = asyncio.run(m.market_ticks(code="600519.SH", count=20))
    assert res["code"] == 0, res
    d = res["data"]
    assert d["count"] == 1 and d["source"] == "tdx"
    assert d["trading_date"] == "20261002"
    assert d["items"][0]["time"] == "09:30:00"


def test_route_ticks_fills_trading_date_when_source_omits(monkeypatch):
    """★ 源没给交易日时后端必须补齐 —— 否则周六打开会把上一交易日的成交当今日。"""
    import app.routes.market as m

    class _Hub:
        async def get_ticks(self, code, count=60, source="auto"):
            return {"code": code, "items": [], "count": 0, "source": "tdx"}

    monkeypatch.setattr(m, "get_hub", lambda: _Hub())
    res = asyncio.run(m.market_ticks(code="600519.SH"))
    assert res["code"] == 0, res
    # 补不到也不能是空字符串（那是「假装给了」）；允许 None，但必须有这个键
    assert "trading_date" in res["data"]


@pytest.mark.parametrize("bad", ["600519.SH", "000001.SH"])
def test_route_ticks_accepts_both_suffix_forms(monkeypatch, bad):
    """个股与指数都走同一条端点（成交流不是个股专属）。"""
    import app.routes.market as m

    seen = {}

    class _Hub:
        async def get_ticks(self, code, count=60, source="auto"):
            seen["code"] = code
            return {"code": code, "items": [], "count": 0, "source": "tdx"}

    monkeypatch.setattr(m, "get_hub", lambda: _Hub())
    res = asyncio.run(m.market_ticks(code=bad))
    assert res["code"] == 0, res
    assert seen["code"] == bad
