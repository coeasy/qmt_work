"""Transports：把 Dialect 产出的 wire 请求送到真正的执行方。"""
from __future__ import annotations

from .base import BaseTransport  # noqa: F401  (re-export)
from .file_signal import FileSignalTransport
from .http_gateway import HttpGatewayTransport
from .inprocess import InProcessTransport
from .lowlatency import RedisTransport, ZmqTransport

__all__ = ["BaseTransport", "InProcessTransport", "FileSignalTransport",
           "RedisTransport", "ZmqTransport", "HttpGatewayTransport", "get_transport"]

_TRANSPORTS: dict[str, type] = {
    InProcessTransport.transport_id: InProcessTransport,
    FileSignalTransport.transport_id: FileSignalTransport,
    RedisTransport.transport_id: RedisTransport,
    ZmqTransport.transport_id: ZmqTransport,
    HttpGatewayTransport.transport_id: HttpGatewayTransport,
}
# ★ 这里是**唯一**的注册表，刻意不提供 ``register_transport`` 那种装饰器：
#   曾经有这么一个「另一个注册入口」，但全仓**零调用** —— 于是真实注册表是上面
#   这个 dict，装饰器只是摆设，两套入口并存只会让人以为「装饰一下就注册好了」。
#   新增 transport 请在此登记；漏登记的后果是 ``get_transport`` 当场抛
#   「未知传输」（响亮失败，不是静默降级），因此不再额外加护栏。


def get_transport(transport_id: str, **kwargs):
    """按 id 实例化 transport；未知 id 直接报错（零 mock：不给默认值糊过去）。"""
    try:
        cls = _TRANSPORTS[transport_id]
    except KeyError as exc:
        raise KeyError(
            f"未知传输: {transport_id}（已注册: {', '.join(sorted(_TRANSPORTS))}）") from exc
    return cls(**kwargs)
