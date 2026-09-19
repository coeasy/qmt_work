"""K 线复权口径透传（V11 R14）。

背景（实测确认的缺陷）
----------------------
``tools/__init__.py::_fetch_broker`` 此前只传 3 个位置参数，而适配器的签名是
``get_kline(code, period, count, start, end, adjust)`` —— **第 6 个参数才是复权口径**。
不传即恒为 ``None`` ⇒ ``dividend_type="none"`` ⇒ **拿到未复权价**。

后果链：
1. ``kline_cache.get_or_fetch`` 仍按**请求口径**落库（``aput(..., adjust="qfq")``）；
2. ``routes/market.py`` 回包 ``"adjust":"qfq"``，前端角标显示「前复权」；
3. 用户看到的是**标着前复权的不复权价** —— 除权日巨大跳空，形态与指标失真。

另一处同源缺陷：``BridgeAdapter.get_kline`` 只收 5 个参数，而
``datasource/registry.py:155`` 是用 ``adjust=adjust`` **关键字**调用的
⇒ 直接 ``TypeError``，整条券商 K 线链路报错。

本文件锁住：
- 请求口径真的传到适配器（qfq/hfq/空 各自如实传递，空不得被吞成 qfq）；
- 「落库口径 == 取数口径」；
- 协议层（ABC）与两个实现都接受 ``adjust``，且 RPC 能把第 6 个参数送到子进程。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import tools as tools_mod


# ---------------------------------------------------------------- 测试替身
class _FakeGateway:
    """模拟券商适配器本体（``bridge.gateway`` 实际就是适配器，非代理）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def get_kline(self, code, period, count, start="", end="", adjust=None):
        self.calls.append((code, period, count, start, end, adjust))
        return [{"time": "20260918", "open": 1.0, "high": 1.1, "low": 0.9,
                 "close": 1.05, "volume": 100, "amount": 1000.0}]


class _FakeBridge:
    def __init__(self, gw):
        self.gateway = gw

    async def call(self, fn, *args, **kwargs):
        # 与 XTQuantBridge.call 同语义：在线程池里 fn(*args, **kwargs)
        return fn(*args, **kwargs)


class _FakeHub:
    """只用于 ``validate_source`` 与 eltdx 回退分支。"""

    def __init__(self, eltdx_ok: bool = False):
        self.eltdx_calls: list = []
        self.eltdx_ok = eltdx_ok

    def validate_source(self, s):
        return s or "auto"

    async def get_kline(self, code, period, count, source=None, conn_id=None,
                        adjust=None):
        self.eltdx_calls.append(adjust)
        if not self.eltdx_ok:
            # 模拟在线源不可用（腾讯限流 501 / 新浪 456 是常态）
            raise RuntimeError("eltdx 不可用（模拟）")
        return [], "eltdx"


class _FakeCache:
    """记录落库口径，证明「落库口径 == 请求口径」。"""

    def __init__(self, bars):
        self.bars = bars
        self.puts: list = []
        self.fetcher_adj: list = []

    async def get_or_fetch(self, code, period, count, fetcher, force=False,
                           adjust=""):
        bars = await fetcher(code, period, count)
        self.puts.append(adjust)
        return {"bars": bars, "source": "broker", "cached_at": None}


@pytest.fixture()
def env(monkeypatch):
    """装好假 bridge / 假 hub，并把 state.kline_cache 置空（走无缓存分支）。"""
    gw = _FakeGateway()
    hub = _FakeHub()
    monkeypatch.setattr(tools_mod, "get_bridge", lambda conn_id=None: _FakeBridge(gw))
    monkeypatch.setattr(tools_mod, "get_hub", lambda: hub)
    monkeypatch.setattr(tools_mod.state, "kline_cache", None, raising=False)
    return gw, hub


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------- 1. 请求口径必须传到适配器
@pytest.mark.parametrize("adjust", ["qfq", "hfq", ""])
def test_requested_adjust_reaches_adapter(env, adjust):
    gw, _ = env
    res = _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10,
                                            source="broker", adjust=adjust))
    assert res["bars"], "假适配器有数据，不应为空"
    assert len(gw.calls) == 1
    assert gw.calls[0][5] == adjust, (
        f"请求口径 {adjust!r} 未如实传给适配器（实得 {gw.calls[0][5]!r}）")


def test_adjust_none_becomes_empty_not_qfq(env):
    """未传 adjust ⇒ 不复权（""）；**绝不能**被默认成 qfq。

    这正是 ``params.get(x) or "qfq"`` 会吞掉空字符串的同类陷阱：
    一旦这里默认成 qfq，用户请求「不复权」会拿到复权价且不自知。
    """
    gw, _ = env
    _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10, source="broker"))
    assert gw.calls[0][5] == "", "adjust=None 必须落成 ''（不复权），不得变成 qfq"


def test_no_adjust_parameter_is_silently_dropped(env):
    """反证：确认适配器**确实**收到了第 6 个参数（而不是被 Python 丢弃）。"""
    gw, _ = env
    _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10,
                                      source="broker", adjust="hfq"))
    code, period, count, start, end, adjust = gw.calls[0]
    assert (code, period, count) == ("600519.SH", "1d", 10)
    assert (start, end, adjust) == ("", "", "hfq")


# ------------------------------------------- 2. 落库口径 == 取数口径（核心不变量）
def test_cache_persist_adjust_matches_fetch_adjust(env, monkeypatch):
    """**复现真实事故路径**：auto + qfq，在线源失败 ⇒ 落到券商 ⇒ 缓存按 qfq 落库。

    修复前这条路径的组合是：适配器收不到 adjust（返回**未复权**价）+ 缓存按
    **qfq** 落库 + 响应标 qfq ⇒ 库里存的是被贴错标签的数据，后续所有读路径
    都被污染。修复后「取数口径 == 落库口径 == 响应口径」。
    """
    gw, hub = env
    cache = _FakeCache([{"time": "20260918", "open": 1.0, "high": 1.1, "low": 0.9,
                         "close": 1.05, "volume": 100, "amount": 1000.0}])
    monkeypatch.setattr(tools_mod.state, "kline_cache", cache, raising=False)

    res = _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10,
                                            source="auto", adjust="qfq"))

    assert hub.eltdx_calls, "auto+qfq 应先尝试在线源"
    assert gw.calls, "在线源失败后必须落到券商"
    assert gw.calls[0][5] == "qfq", "取数口径：券商必须真的按 qfq 取"
    assert cache.puts == ["qfq"], "落库口径必须与取数口径一致"
    assert res["bars"], "应返回券商数据"


def test_cache_path_empty_adjust_stays_empty(env, monkeypatch):
    """不复权请求经缓存路径时，落库口径仍是 ''（不得被兜底成 qfq）。"""
    gw, _ = env
    cache = _FakeCache([{"time": "20260918", "open": 1.0, "high": 1.1, "low": 0.9,
                         "close": 1.05, "volume": 100, "amount": 1000.0}])
    monkeypatch.setattr(tools_mod.state, "kline_cache", cache, raising=False)

    _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10,
                                      source="auto", adjust=""))

    assert gw.calls[0][5] == ""
    assert cache.puts == [""]


def test_empty_eltdx_falls_through_to_broker(env, monkeypatch):
    """**空结果 = 失败**：eltdx 返回空（不抛异常）时也必须继续走券商。

    修复前该分支直接 `return {"bars": [], "source": None}`，于是 auto 链
    **从不回退券商**：实测请求 ``adj=qfq`` 时路由拿到 source=None + 0 根，
    随即降级到 ``local:sqlite`` —— 用户拿到的既不是券商 qfq 也不是在线源，
    而是本地可能很旧的数据（且被标成「前复权」）。
    """
    gw, hub = env
    hub.eltdx_ok = True  # eltdx 「成功」但返回空
    cache = _FakeCache([{"time": "20260918", "open": 1.0, "high": 1.1, "low": 0.9,
                         "close": 1.05, "volume": 100, "amount": 1000.0}])
    monkeypatch.setattr(tools_mod.state, "kline_cache", cache, raising=False)

    res = _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10,
                                            source="auto", adjust="qfq"))

    assert gw.calls, "eltdx 返回空后必须继续尝试券商（不得直接 return 空）"
    assert gw.calls[0][5] == "qfq", "回退到券商时仍须带上请求口径"
    assert res["bars"], "应返回券商数据"


def test_explicit_eltdx_empty_does_not_switch_source(env):
    """显式 source=eltdx 时不得改源：如实返回空，由路由决定兜底。"""
    gw, hub = env
    hub.eltdx_ok = True
    res = _run(tools_mod.fetch_kline_cached("600519.SH", "1d", 10,
                                            source="eltdx", adjust="qfq"))
    assert res["bars"] == []
    assert not gw.calls, "显式 eltdx 不得偷偷改走券商"


# ------------------------------------------------- 3. 协议层与实现签名
def test_gateway_protocol_accepts_adjust_keyword():
    """``registry.py:155`` 用 ``adjust=adjust`` 关键字调用 ⇒ 签名必须收得下。"""
    from xtquant_client.gateway import XTQuantGateway

    sig = inspect.signature(XTQuantGateway.get_kline)
    assert "adjust" in sig.parameters
    assert sig.parameters["adjust"].default is None


def test_broker_adapter_base_accepts_adjust():
    from xtquant_client.base import BrokerAdapter

    sig = inspect.signature(BrokerAdapter.get_kline)
    assert "adjust" in sig.parameters


def test_xtp_adapter_accepts_adjust_positionally():
    """RPC 子进程按**位置**传 6 个参数 ⇒ 真实实现必须能收下第 6 个。"""
    from xtquant_client.xtp.quotes import QuotesMixin

    params = list(inspect.signature(QuotesMixin.get_kline).parameters)
    assert params[:6] == ["self", "code", "period", "count", "start", "end"]
    assert params[6] == "adjust"


def test_bridge_adapter_forwards_adjust_over_rpc():
    """BridgeAdapter（子进程托管模式）必须把 adjust 放进 RPC 参数列表。"""
    from xtquant_client.bridge_client import BridgeAdapter

    adapter = BridgeAdapter.__new__(BridgeAdapter)
    seen: dict = {}

    def _rpc(method, args, timeout=30.0):
        seen["method"] = method
        seen["args"] = list(args)
        return []

    adapter._rpc = _rpc  # type: ignore[method-assign]
    adapter.get_kline("600519.SH", "1d", 10, "", "", "qfq")

    assert seen["method"] == "get_kline"
    assert seen["args"] == ["600519.SH", "1d", 10, "", "", "qfq"]


def test_bridge_adapter_adjust_none_sends_empty():
    from xtquant_client.bridge_client import BridgeAdapter

    adapter = BridgeAdapter.__new__(BridgeAdapter)
    seen: dict = {}
    adapter._rpc = lambda method, args, timeout=30.0: seen.update(args=list(args)) or []
    adapter.get_kline("600519.SH", "1d", 10)

    assert seen["args"][-1] == "", "None 必须序列化成 ''，子进程才能落成 dividend_type=none"


def test_stub_adapter_accepts_adjust():
    """未接入 SDK 的适配器桩也要收得下 adjust（否则 RPC 分发 TypeError）。"""
    from xtquant_client.adapters import ExternalBrokerAdapter

    params = list(inspect.signature(ExternalBrokerAdapter.get_kline).parameters)
    assert "adjust" in params, "桩适配器缺 adjust 会让 RPC 反射调用直接 TypeError"
