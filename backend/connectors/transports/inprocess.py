"""进程内直连传输：包住 adapter + XTQuantBridge 的既有线程模型。

★ 刻意**复用** ``XTQuantBridge`` 的线程池而不是自己 ``asyncio.to_thread``：
``to_thread`` 用的是解释器默认 executor，其线程**非 daemon**，解释器退出时会 join；
把「可能无界阻塞」的 SDK 调用丢给它，wait_for 能超时返回但**进程退不出去**（TD-25 事故）。
XTQuantBridge 的池在 ``stop()`` 里 shutdown(wait=False, cancel_futures=True)，是有界停机。

零引入新线程语义 —— 这是本 transport 存在的最大理由。
"""
from __future__ import annotations

from typing import Any

from ..events import PushEventSource
from ..ports import EventSemanticsSpec
from ..transport import WireRequest, WireResponse
from .base import BaseTransport
from .semantics import spec_for


class InProcessTransport(BaseTransport):
    """同进程调用 adapter（含 BridgeAdapter 这种「adapter 内部再走 IPC」的形态）。

    ``write_ops`` 是**方言指令名**的白名单（由 dialect 提供），用来决定走
    ``call_locked``（串行）还是 ``call``（并发）。默认值即 XtQuant 的写指令集合。
    """

    transport_id = "inprocess"

    #: 进程内直连：回调式订阅可用（on_tick 在同一进程内被调用）。
    supports_callback = True

    #: XtQuant adapter 的写方法名默认集合（deprecated 用法，优先由 dialect 传入）。
    DEFAULT_WRITE_OPS = frozenset({
        "place_order", "cancel_order", "subscribe_quote", "cancel_order_price",
    })

    def __init__(self, adapter: Any, bridge: Any = None,
                 write_ops: frozenset[str] | None = None):
        self.adapter = adapter
        if bridge is None:
            from xtquant_client.gateway import XTQuantBridge

            bridge = XTQuantBridge(adapter)
        self.bridge = bridge
        self.write_ops = frozenset(write_ops or self.DEFAULT_WRITE_OPS)
        # EventPort（PUSH）：miniQMT 有原生回调，这里把回调喂进 PushEventSource，
        # 使 GenericConnector.stream_events() 对进程内直连也真正出事件。
        self._push = PushEventSource()

    async def start(self) -> None:
        await self.bridge.start()

    # ------------------------------------------------------------------
    # EventPort（PUSH）
    # ------------------------------------------------------------------
    def event_semantics(self) -> EventSemanticsSpec:
        return spec_for(self.transport_id)

    def push_event(self, kind: str, data: dict) -> None:
        """外部（gateway）把 adapter 的 on_order/on_trade 回调转交到这里。"""
        self._push.push(kind, data)

    def bind_adapter_callbacks(self) -> None:
        """若 adapter 实现了 on_order/on_trade 回调注册，挂到本传输的推送源。

        适配器默认 no-op（``BrokerAdapter.on_order`` 有意留空），故无回调时安全跳过。
        """
        adapter = self.adapter
        for agent_kind, method in (("order", "on_order"), ("trade", "on_trade")):
            reg = getattr(adapter, method, None)
            if callable(reg):
                try:
                    reg(lambda data, _k=agent_kind: self.push_event(_k, data))
                except Exception:  # noqa: BLE001  注册失败不应影响主路径
                    pass

    def stream_events(self) -> list:
        return self._push.stream_events()

    async def _dispatch(self, request: WireRequest, started: float) -> WireResponse:
        method_name = request.op
        fn = getattr(self.adapter, method_name, None)
        if fn is None:
            # 能力问题，不是调用失败：error_type 明确为 UnsupportedSelector。
            return self._fail(
                f"adapter 无该方法: {method_name}", "Unsupported",
                request, started)

        params = dict(request.params or {})
        # 写操作串行化（call_locked）；读操作并发（call）。
        # 判定来源是规范化 op 的 ``request.write``（见 dialects.is_write_op），
        # 不再依赖 adapter 方法名匹配 —— 大小 QMT 方言动作名不同（place_order vs
        # PLACE），靠方法名白名单会错位；同时保留白名单作为兜底，保证旧路径不变。
        is_write = bool(getattr(request, "write", False)) or (method_name in self.write_ops)
        try:
            if is_write:
                raw = await self.bridge.call_locked(fn, **params)
            else:
                raw = await self.bridge.call(fn, **params)
        except TypeError as exc:
            # 参数签名不匹配是**装配 bug**，必须炸出来而不是吞掉（easytrader #520 教训）。
            return self._fail(f"{method_name} 参数不匹配: {exc}", "SignatureMismatch",
                              request, started)
        return self._ok(raw, request, started)

    async def close(self) -> None:
        await self.bridge.stop()


__all__ = ["InProcessTransport"]
