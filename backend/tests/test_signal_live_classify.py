"""SignalRouter._live 错误分类契约：**券商不可用**必须打 `broker_unavailable`。

路由层（app/routes/trade.py）据此把「没有可用券商」映射为 503 + 「券商连接」引导，
而把「券商可用但柜台/风控拒单」保留为真实的执行拒绝（400）。两者若混同，会把
「去连券商」的可操作指引淹没在错误码里（或反向：把拒单误报成服务不可用）。
"""
import asyncio

from gateway.signal_router import SignalRouter
from xtquant_client.base import (
    BrokerError,
    BrokerNotConnectedError,
    BrokerSDKError,
)


class _PermissiveRisk:
    def check_order(self, code, price, volume, side, price_type="limit"):
        return True, ""


class _BoomBridge:
    """网关桩：真实调用路径走通，最终 place_order 抛指定异常。"""

    def __init__(self, exc):
        self._exc = exc
        self.gateway = self

    def get_quote(self, code):
        return {"last": 10.0}

    def place_order(self, *args):
        raise self._exc

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)


class _Mgr:
    def __init__(self, bridge):
        self._bridge = bridge

    def bridge(self, conn_id=None):
        return self._bridge


def _submit(exc_or_bridge):
    bridge = (exc_or_bridge if isinstance(exc_or_bridge, _BoomBridge)
              else _BoomBridge(exc_or_bridge))
    router = SignalRouter(_Mgr(bridge), risk=_PermissiveRisk())
    router.mode = "live"
    return asyncio.run(router.submit("600519.SH", "buy", 100, 10.0, "limit",
                                     source="manual", auto_confirm=True))


def test_broker_sdk_error_marks_unavailable():
    out = _submit(BrokerSDKError("xtquant", "pip install xtquant"))
    assert out["ok"] is False
    assert out["broker_unavailable"] is True
    assert out["error_type"] == "BrokerSDKError"


def test_not_connected_marks_unavailable():
    out = _submit(BrokerNotConnectedError("未连接券商客户端"))
    assert out["ok"] is False
    assert out["broker_unavailable"] is True


def test_counter_rejection_is_not_broker_unavailable():
    out = _submit(BrokerError("下单失败：柜台返回 -1（资金不足/非交易时段/无权限）"))
    assert out["ok"] is False
    assert out["broker_unavailable"] is False
    assert out["error_type"] == "BrokerError"
    assert "柜台返回 -1" in out["reason"]


def test_no_bridge_marks_unavailable():
    router = SignalRouter(_Mgr(None), risk=_PermissiveRisk())
    router.mode = "live"
    out = asyncio.run(router.submit("600519.SH", "buy", 100, 10.0, "limit",
                                    auto_confirm=True))
    assert out["ok"] is False
    assert out["broker_unavailable"] is True
    assert out["error_type"] == "BrokerNotConnectedError"
