"""券商异常跨进程重建（回归 P0）：消息必须**原样保留**，绝不二次套娃。

背景：桥接子进程回传 `{"ok": false, "error": <完整可读文案>, "error_type": ...}`。
早期客户端用 `BrokerSDKError(msg)` 重建——而 `BrokerSDKError(sdk, extra)` 是双参构造，
于是整段文案被当成「SDK 名」再包一层，产出
「缺少券商 SDK：缺少券商 SDK：xtquant。请…安装（下单失败：柜台返回 -1…），且…
状态。。请在运行本后端的机器上安装（参见券商文档），且…状态。」这类重复且自相矛盾的
错误说明（真实故障：POST /api/v1/trade/order 返回该文案）。
"""
from xtquant_client.base import (
    BrokerError,
    BrokerNotConnectedError,
    BrokerSDKError,
)
from xtquant_client.bridge_client import _rebuild_error


def test_sdk_error_from_message_is_not_rewrapped():
    msg = ("缺少券商 SDK：xtquant。请在运行本后端的机器上安装（pip install xtquant），"
           "且券商客户端需处于登录/可交易状态。")
    err = _rebuild_error("BrokerSDKError", msg)
    assert isinstance(err, BrokerSDKError)
    assert str(err) == msg
    assert str(err).count("缺少券商 SDK") == 1
    assert "参见券商文档" not in str(err)


def test_broker_error_rebuild_preserves_message_and_type():
    msg = "下单失败：柜台返回 -1（资金不足/标的不在交易时段/无交易权限/风控拦截）"
    err = _rebuild_error("BrokerError", msg)
    assert type(err) is BrokerError
    assert not isinstance(err, BrokerSDKError)
    assert str(err) == msg


def test_not_connected_rebuild():
    err = _rebuild_error("BrokerNotConnectedError", "桥接子进程未运行")
    assert isinstance(err, BrokerNotConnectedError)
    assert str(err) == "桥接子进程未运行"


def test_unknown_error_type_falls_back_to_broker_error():
    err = _rebuild_error("SomeFutureError", "x")
    assert type(err) is BrokerError
    assert str(err) == "x"
