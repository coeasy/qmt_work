"""各传输的事件语义契约（延迟上界是硬指标，不是文档修辞）。

为什么单独一个模块：``max_latency_ms`` 会被三处消费 ——

1. ``EventPort.event_semantics()`` 回答上层；
2. Probe 面把它放进能力诊断，让前端知道「这个连接能有多快」；
3. **最关键的** ``order_watchdog`` 用它推导超时守护窗口。

集中定义才能保证三者看到的是同一个数。若把上界散落在各 transport 类里，
改一处忘两处就会导致「未回报但实际已成交」的误撤单。

上界取值依据
------------
* ``file``  agent 轮询间隔（默认 0.5s）×2 + 本端轮询间隔 → 保守 1500ms；
* ``redis`` pub/sub 推送，可能丢帧（对端重启即丢）→ PUSH_WITH_GAP，100ms；
* ``zmq``   DEALER/ROUTER 原生路由，同机极低延迟 → PUSH_WITH_GAP，50ms；
* ``poll``  兜底：由 PollDiffEventSource 按实际轮询间隔动态计算。
"""
from __future__ import annotations

from ..ports import EventSemantics, EventSemanticsSpec

#: transport_id → 事件语义 + 上界
TRANSPORT_SEMANTICS: dict[str, EventSemanticsSpec] = {
    "file": EventSemanticsSpec(
        semantics=EventSemantics.POLL_DIFF,
        max_latency_ms=1500,
        poll_interval_ms=500,
    ),
    "redis": EventSemanticsSpec(
        semantics=EventSemantics.PUSH_WITH_GAP,
        max_latency_ms=100,
    ),
    "zmq": EventSemanticsSpec(
        semantics=EventSemantics.PUSH_WITH_GAP,
        max_latency_ms=50,
    ),
    "http": EventSemanticsSpec(
        semantics=EventSemantics.PUSH_WITH_GAP,
        max_latency_ms=200,
    ),
    "inprocess": EventSemanticsSpec(
        semantics=EventSemantics.PUSH,
    ),
}

_UNSET = EventSemanticsSpec(semantics=EventSemantics.NONE)


def spec_for(transport_id: str) -> EventSemanticsSpec:
    """按 transport_id 取语义；未知传输答 NONE（诚实，不猜）。"""
    return TRANSPORT_SEMANTICS.get(transport_id, _UNSET)


def max_latency_ms(transport_id: str) -> int:
    return spec_for(transport_id).max_latency_ms


__all__ = ["TRANSPORT_SEMANTICS", "spec_for", "max_latency_ms"]
