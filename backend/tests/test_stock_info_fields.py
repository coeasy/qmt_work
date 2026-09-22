"""`GET /market/stock-info` 的**字段契约** —— 「基本信息」面板的数据来源。

## 为什么锁这个

面板要显示市值 / PE / PB / 换手 / 振幅 / 量比 / 均价，全都靠这个端点透出。
两条必须守住的性质：

1. **拿到就透出**：详情层给了的字段，端点必须原样返回（否则前端永远 `--`）；
2. **没拿到就是 `null`**，不是 `0`：前端 `fmtAmount(null)` → `--`；
   若端点返回 `0`，界面会显示「总市值 0」——**假数据比没数据更危险**。

⚠️ 没装 pytest-asyncio ⇒ 异步一律 sync + ``asyncio.run(...)``。
"""
import asyncio

import pytest

from app.routes import market as market_routes
from app.routes.market import market_stock_info
from datasource.base import EXT_DETAIL_KEYS

FULL_DETAIL = {
    "name": "贵州茅台",
    "exchange": "上海证券交易所",
    "high_limit": 1393.68,
    "low_limit": 1140.28,
    "pre_close": 1266.98,
    "industry": "酿酒行业",
    "concepts": ["白酒", "MSCI"],
    "source": "tencent",
    # 扩展字段
    "open": 1262.99,
    "high": 1265.88,
    "low": 1256.10,
    "avg_price": 1259.84,
    "amplitude": 0.77,
    "turnover_rate": 0.20,
    "volume_ratio": 1.14,
    "pe_ttm": 19.30,
    "pb": 6.25,
    "circ_mv": 15715.03 * 1e8,
    "total_mv": 15715.03 * 1e8,
    "amount": 313585 * 1e4,
}

EXT_KEYS = ("open", "high", "low", "avg_price", "amplitude", "turnover_rate",
            "volume_ratio", "pe_ttm", "pb", "circ_mv", "total_mv", "amount")


class _StubHub:
    """假 hub：详情走 `det`，行情走 `quotes`（用于验证「缺口补齐」）。"""

    def __init__(self, det=None, exc=None, quotes=None, plugins=None):
        self.det = det
        self.exc = exc
        self.quotes = quotes or {}
        self._plugins = plugins or {}
        self.calls = []
        self.quote_calls = []

    async def get_instrument_detail(self, code, source="auto", conn_id=None):
        self.calls.append((code, source, conn_id))
        if self.exc:
            raise self.exc
        return self.det

    async def get_quote(self, code, source="auto", conn_id=None):
        self.quote_calls.append((code, source, conn_id))
        return self.quotes.get(source)


def _metrics_plugin():
    """假装一个公开行情源：它的 `_DETAIL_KEYS` 覆盖全部行情派生字段。"""

    class _Plugin:
        _DETAIL_KEYS = ("name", "pre_close", "last") + EXT_DETAIL_KEYS

    return _Plugin()


# 本地 TDX 形态的详情：有名称/行业/概念/涨跌停，**没有**市值 / PE / PB / 换手…
ELTDX_DETAIL = {
    "name": "贵州茅台",
    "exchange": "上交所",
    "high_limit": 1393.68,
    "low_limit": 1140.28,
    "pre_close": 1266.98,
    "industry": "酿酒",
    "concepts": ["白酒概念"],
    "source": "eltdx",
}

TENCENT_QUOTE = {
    "code": "600519.SH",
    "last": 1257.12,
    "open": 1262.99,
    "high": 1265.88,
    "low": 1256.10,
    "avg_price": 1259.84,
    "amplitude": 0.77,
    "turnover_rate": 0.20,
    "volume_ratio": 1.14,
    "pe_ttm": 19.30,
    "pb": 6.25,
    "circ_mv": 15715.03 * 1e8,
    "total_mv": 15715.03 * 1e8,
    "amount": 313585 * 1e4,
    "source": "tencent",
}


@pytest.fixture
def patch_hub(monkeypatch):
    def _install(hub):
        monkeypatch.setattr(market_routes, "get_hub", lambda: hub)
        return hub
    return _install


def _payload(resp):
    return resp.get("data") if isinstance(resp, dict) else resp


def test_extended_fields_are_passed_through(patch_hub):
    patch_hub(_StubHub(FULL_DETAIL))
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    assert info is not None
    for key in EXT_KEYS:
        assert info[key] == FULL_DETAIL[key], f"{key} 应原样透出"
    assert info["name"] == "贵州茅台"
    assert info["industry"] == "酿酒行业"
    assert info["concepts"] == ["白酒", "MSCI"]


def test_missing_fields_stay_null_not_zero(patch_hub):
    """详情层只有少数几个键时，其余必须是 **None**（前端 `--`），绝不能是 0。"""
    patch_hub(_StubHub({"name": "平安银行", "exchange": "深交所", "pre_close": 11.66}))
    info = _payload(asyncio.run(market_stock_info("000001.SZ", ctx=None)))
    for key in EXT_KEYS:
        assert info[key] is None, f"{key} 未取到时应为 None，实际 {info[key]!r}"
        assert info[key] != 0
    assert info["high_limit"] is None
    assert info["low_limit"] is None


def test_all_sources_down_keeps_note_and_nulls(patch_hub):
    """全源不可用：返回 note 说明降级，数值字段全 None（板块仍按代码前缀推断）。"""
    from datasource.registry import MarketDataUnavailable

    patch_hub(_StubHub(exc=MarketDataUnavailable("no source")))
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    assert info["note"], "全源不可用必须给出 note（否则「推断的板块」会被当成事实）"
    assert info["board"], "板块仍按代码前缀推断"
    for key in EXT_KEYS + ("high_limit", "low_limit", "pre_close"):
        assert info[key] is None


def test_bare_code_is_suffixed_before_lookup(patch_hub):
    """裸 6 位代码必须被补后缀后再查 —— 否则查不到中文名，界面只剩数字。"""
    hub = _StubHub(FULL_DETAIL)
    patch_hub(hub)
    _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    assert hub.calls[0][0] == "600519.SH"


def test_manager_passes_extended_fields_through():
    """★ manager 层必须把插件的扩展字段**原样透出**。

    这是 2026-09-21 真实踩到的坑：插件层（腾讯快照）已经解析出市值 / PE / PB / 换手…
    但 manager 的 ``_plugin_detail`` 只挑固定几个键拼返回体 ⇒ 新字段在这一步被丢掉，
    端点恒返回 ``null``，界面一片 ``--``。

    ⚠️ 而**直接调插件的单测是全绿的**（它绕过了 manager）—— 所以必须补这一层测试。
    """
    from datasource.base import EXT_DETAIL_KEYS, DataSource
    from datasource.registry import DataSourceManager

    class _Src(DataSource):
        name = "stub"
        capabilities = frozenset({"quote", "instrument_detail"})

        async def get_instrument_detail(self, code):
            return dict(FULL_DETAIL)

        # 抽象方法（本用例用不到，但 DataSource 是 ABC，必须实现才能实例化）
        async def get_quote(self, code):
            return None

        async def get_kline(self, code, period="1d", count=250, **kw):
            return None

        async def get_stock_list(self):
            return None

    mgr = DataSourceManager().register(_Src())
    det = asyncio.run(mgr.get_instrument_detail("600519.SH", source="stub"))
    assert det is not None
    for key in EXT_DETAIL_KEYS:
        assert key in det, f"manager 丢掉了扩展字段 {key}"
        assert det[key] == FULL_DETAIL[key]


def test_gap_filled_from_metrics_source_when_detail_lacks_them(patch_hub):
    """★ 打包版实测形态：详情源是 eltdx（**没有**市值/PE/PB/换手），必须从公开行情源补齐。

    不补的话，用户装的是自带 eltdx 的客户端 ⇒ 面板上这 12 个字段全是 `--`，功能等于没做。
    """
    hub = _StubHub(ELTDX_DETAIL, quotes={"tencent": TENCENT_QUOTE},
                   plugins={"eltdx": object(), "tencent": _metrics_plugin()})
    patch_hub(hub)
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))

    for key in ("open", "high", "low", "avg_price", "amplitude", "turnover_rate",
                "volume_ratio", "pe_ttm", "pb", "circ_mv", "total_mv", "amount"):
        assert info[key] == TENCENT_QUOTE[key], f"{key} 没被补齐"
    assert info["metrics_source"] == "tencent"
    # 详情源自己的字段不能被覆盖（涨跌停来自 eltdx 的推算）
    assert info["high_limit"] == 1393.68
    assert info["industry"] == "酿酒"


def test_no_extra_quote_call_when_detail_already_has_metrics(patch_hub):
    """详情源已经给全了 ⇒ **不许**再多打一次行情（省掉一次无谓请求）。"""
    hub = _StubHub({**ELTDX_DETAIL, **{k: TENCENT_QUOTE[k] for k in (
        "open", "high", "low", "avg_price", "amplitude", "turnover_rate",
        "volume_ratio", "pe_ttm", "pb", "circ_mv", "total_mv", "amount")}},
        quotes={"tencent": TENCENT_QUOTE},
        plugins={"tencent": _metrics_plugin()})
    patch_hub(hub)
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    assert hub.quote_calls == [], f"不该补请求，实际打了 {hub.quote_calls}"
    assert info["metrics_source"] is None


def test_gap_stays_null_when_no_metrics_source(patch_hub):
    """一个能提供派生字段的源都没有（离线）⇒ 保持 None，前端 `--`，不许填 0。"""
    hub = _StubHub(ELTDX_DETAIL, plugins={"eltdx": object()})
    patch_hub(hub)
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    for key in ("open", "high", "low", "avg_price", "amplitude", "turnover_rate",
                "volume_ratio", "pe_ttm", "pb", "circ_mv", "total_mv", "amount"):
        assert info[key] is None
    assert info["metrics_source"] is None


def test_gap_fill_failure_does_not_break_endpoint(patch_hub):
    """补齐过程抛异常 ⇒ 接口照常返回详情数据（补齐是尽力而为）。"""
    class _BoomHub(_StubHub):
        async def get_quote(self, code, source="auto", conn_id=None):
            raise RuntimeError("公开源挂了")

    patch_hub(_BoomHub(ELTDX_DETAIL, plugins={"tencent": _metrics_plugin()}))
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    assert info["name"] == "贵州茅台"
    assert info["high_limit"] == 1393.68
    assert info["pe_ttm"] is None


def test_manager_and_plugin_share_one_key_list():
    """插件取字段与 manager 透字段必须**同一份常量** —— 两边各写一份就会漂移。"""
    from datasource.base import EXT_DETAIL_KEYS
    from datasource.public_sources import _PublicSource

    for key in EXT_DETAIL_KEYS:
        assert key in _PublicSource._DETAIL_KEYS, f"插件键集缺 {key}"


# ---------------------------------------------------------------------------
# 打包态实测（2026-09-21）：全新安装的客户端上，`get_instrument_detail` 会返回 **None**
# ---------------------------------------------------------------------------
#
# ⚠️ 「全链都不可用」与「全链都返回空壳」是两回事：
#    前者抛 `MarketDataUnavailable`（上面 `test_all_sources_down_keeps_note_and_nulls` 覆盖），
#    后者走到 `return last`，而 last 仍是 `None`。
#    场景：没连券商 + 本地没下载过 TDX 数据 —— 也就是**刚装好的客户端**。
#    此前端点直接 `det.get(...)` ⇒ `AttributeError` ⇒ **HTTP 500**，
#    而右侧「基本信息」恰恰是要「快速查看」的面板：一点开就报错。


def test_detail_none_degrades_instead_of_crashing(patch_hub):
    """`det` 为 None 时必须优雅降级（不是 500），并诚实说明为什么是空的。"""
    patch_hub(_StubHub(None))
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    assert isinstance(info, dict), "det 为 None 时端点必须仍返回结构，而不是抛异常"
    assert info["note"], "无本地画像必须给 note（否则用户以为是自己代码写错了）"
    for key in EXT_KEYS + ("high_limit", "low_limit", "pre_close"):
        assert info[key] is None
    assert info["board"], "板块仍按代码前缀推断（不依赖任何源）"
    # 名称只能来自名称表或留空，**绝不许**拿代码冒充（前端会渲染成 `600519.SH` 看着像名字）
    assert info["name"] != "600519.SH"


def test_detail_none_still_fills_metrics_from_public_source(patch_hub):
    """没有本地画像**不等于**没有行情：公开源与本地 TDX 数据无关，仍应补上实时字段。"""
    hub = _StubHub(None, quotes={"tencent": TENCENT_QUOTE},
                   plugins={"eltdx": object(), "tencent": _metrics_plugin()})
    patch_hub(hub)
    info = _payload(asyncio.run(market_stock_info("600519", ctx=None)))
    for key in EXT_KEYS:
        assert info[key] == TENCENT_QUOTE[key], f"{key} 应从公开行情源补齐"
    assert info["metrics_source"] == "tencent"
    assert info["high_limit"] is None, "涨跌停属于画像字段，公开快照没给就保持 None"
