"""按日期区间取 K 线（``DataSourceManager.get_kline_range``）测试。

这是「全量日线回补」的取数入口（V11 §5.3 P0-3 III）。它存在的唯一理由是：
**免费在线源只接受 ``count``（最近 N 根），把 ``start``/``end`` 传过去会被静默
忽略** —— 于是「逐年翻页」变成「同一批最近数据重复 12 遍」，而调用方毫不知情。
所以这里钉死两条：

1. ``start``/``end`` 必须**真的**传到源（不是收下就扔）；
2. 链上**只有声明了** ``supports_kline_range`` 的源才会被调用；没有这样的源时
   返回 ``(None, None)`` 让调用方如实退化，**绝不**退而求其次去问一个会静默
   忽略区间的源。

测试替身源，无 eltdx、无网络。
"""
import asyncio

from datasource.base import DataSource
from datasource.registry import DataSourceManager

from _phase4_support import force_deps  # noqa: F401  —— 保持与其余 datasource 测试一致的依赖前置


class _RangeBroker:
    """声明了区间能力的券商替身：记录收到的 start/end。"""

    supports_kline_range = True

    def __init__(self):
        self.calls: list = []

    async def get_kline(self, code, period="1d", count=250, adjust=None,
                        start="", end=""):
        self.calls.append((code, start, end, adjust))
        return [{"time": "20240101", "close": 1.0}]


class _CountOnlyBroker:
    """**没有**声明区间能力的券商替身（模拟只有 count 的源）。"""

    def __init__(self):
        self.calls: list = []

    async def get_kline(self, code, period="1d", count=250, adjust=None,
                        start="", end=""):
        self.calls.append((code, start, end))
        return [{"time": "20260815", "close": 9.9}]


class _CountOnlyPlugin(DataSource):
    """只接受 count 的插件源（eltdx / 免费在线源同形）。

    ★ 签名里**没有** ``start``/``end`` —— 这正是现网在线源的真实形态，
    也是「把区间交给它会被静默忽略」的根源。
    """

    name = "eltdx"
    #: 声明 kline_qfq 才会被 ``_resolve_sources`` 收进候选链
    capabilities = frozenset({"quote", "kline", "kline_qfq", "kline_hfq",
                              "instrument_detail", "stock_list"})

    def __init__(self):
        self.calls: list = []

    async def get_quote(self, code):
        return None

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        self.calls.append((code, count))
        return [{"time": "20260815", "close": 9.9}]

    async def get_instrument_detail(self, code):
        return None

    async def get_stock_list(self):
        return None


class _RangePlugin(_CountOnlyPlugin):
    """假想的「未来的区间插件源」：声明能力 **且** 签名收区间。"""

    name = "rangelib"
    supports_kline_range = True

    async def get_kline(self, code, period="1d", count=250, adjust=None,
                        start="", end=""):
        self.calls.append((code, start, end))
        return [{"time": "20130615", "close": 9.9}]


class _LyingPlugin(_CountOnlyPlugin):
    """**声明**了区间能力，但签名不认 ``start``/``end`` —— 口惠而实不至。"""

    name = "lying"
    supports_kline_range = True


def test_range_is_forwarded_to_broker():
    async def c():
        broker = _RangeBroker()
        m = DataSourceManager()
        m.register_broker(lambda cid: broker)
        m.set_auto_chain(["broker", "eltdx"])

        bars, src = await m.get_kline_range(
            "600519.SH", "1d", adjust="qfq", start="20230101", end="20231231")
        assert src == "broker"
        assert bars and bars[0]["time"] == "20240101"
        # ★ 区间必须真的传下去 —— 收下就扔等于「假装翻页」
        assert broker.calls == [("600519.SH", "20230101", "20231231", "qfq")]
    asyncio.run(c())


def test_range_skips_sources_without_the_capability():
    """没有声明区间能力的源**一个都不许调**。

    这是本方法存在的全部意义：把 start/end 交给一个只认 count 的源，
    它会静默忽略并返回「最近 N 根」—— 调用方以为拿到了 2013 年的历史。
    """
    async def c():
        broker = _CountOnlyBroker()
        plugin = _CountOnlyPlugin()
        m = DataSourceManager()
        m.register_broker(lambda cid: broker)
        m.register(plugin)
        m.set_auto_chain(["broker", "eltdx"])

        bars, src = await m.get_kline_range(
            "600519.SH", "1d", start="20130101", end="20131231")

        assert bars is None and src is None
        assert broker.calls == [], "未声明区间能力的券商源不得被调用"
        assert plugin.calls == [], "只认 count 的在线源不得被调用"
    asyncio.run(c())


def test_range_falls_through_to_next_capable_source():
    """链上前一个源不可用（无连接）时，继续找下一个声明了区间能力的源。"""
    async def c():
        plugin = _RangePlugin()
        m = DataSourceManager()
        m.register_broker(lambda cid: None)          # 无券商连接
        m.register(plugin)
        m.set_auto_chain(["broker", "rangelib"])

        bars, src = await m.get_kline_range(
            "600519.SH", "1d", start="20130101", end="20131231")
        assert src == "rangelib"
        assert bars and bars[0]["time"] == "20130615"
        assert plugin.calls == [("600519.SH", "20130101", "20131231")]
    asyncio.run(c())


def test_range_skips_source_that_declares_but_cannot_accept():
    """声明了区间能力、签名却不收 ``start``/``end`` ⇒ 判为不支持。

    若不挡这一下，``src.get_kline(..., start=...)`` 会在**协程创建处**抛
    ``TypeError``（在 ``_call_source`` 的 try 之外）⇒ 整条链当场断掉，
    表现为「全量回补永远不生效」，日志里只有一条 debug 级异常。
    """
    async def c():
        lying = _LyingPlugin()
        m = DataSourceManager()
        m.register_broker(lambda cid: None)
        m.register(lying)
        m.set_auto_chain(["broker", "lying"])

        bars, src = await m.get_kline_range(
            "600519.SH", "1d", start="20130101", end="20131231")
        assert bars is None and src is None
        assert lying.calls == []
    asyncio.run(c())


def test_accepts_kline_range_helper():
    from datasource.registry import _accepts_kline_range

    assert _accepts_kline_range(_RangeBroker()) is True
    assert _accepts_kline_range(_CountOnlyBroker()) is False    # 签名收了但没声明
    assert _accepts_kline_range(_CountOnlyPlugin()) is False    # 没声明也没签名
    assert _accepts_kline_range(_LyingPlugin()) is False        # 声明了但签名不收

    class _Kwargs:
        supports_kline_range = True

        async def get_kline(self, *a, **kw):
            return []

    assert _accepts_kline_range(_Kwargs()) is True              # **kwargs 能收下


def test_bound_broker_source_declares_range_capability():
    """``_BoundBrokerSource`` 必须声明区间能力 —— 回补能不能走通全看这个标志。"""
    from datasource.registry import _BoundBrokerSource

    assert getattr(_BoundBrokerSource, "supports_kline_range", False) is True


def test_plugin_sources_do_not_declare_range_capability():
    """现网插件源（eltdx 等）**不得**声明区间能力，否则回补会假翻页。"""
    from datasource.registry import DataSourceManager

    m = DataSourceManager()
    for name, src in m._plugins.items():
        assert not getattr(src, "supports_kline_range", False), (
            f"源 {name} 声明了 supports_kline_range，但它只接受 count —— "
            "请先实现真正的区间取数再声明")
