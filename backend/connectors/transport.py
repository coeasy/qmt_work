"""Transport 抽象：只管「怎么把请求送到」，不知道任何方言语义（V4 §3）。

为什么把 Transport 从 Connector 里拆出来：
同一个客户端常常有**多种接法**（进程内直连 / 子进程桥 / 文件桥 / Redis / ZMQ），
而它们的**方言内容完全一致**。若不拆，每一种接法都要把「动词 + 字段提取 + 错误映射」
重写一遍 —— 这正是组合能消除的重复。

职责边界
--------
* **Dialect**  负责「说什么」：canonical 概念 → 方言指令 + 参数；方言返回 → canonical 模型。
* **Transport** 负责「怎么送」：把 wire 请求送到执行方并取回 wire 响应。
  它**不认识** xtquant，也不认识 passorder。

失败语义（零 mock）
------------------
* **传输故障**（对端不可达 / 超时 / 序列化失败）⇒ raise ``TransportError``；
  上层映射为 ``BrokerNotConnectedError`` → HTTP 503 + 引导。
* **业务失败**（柜台拒单 / 注入函数缺失）⇒ 返回 ``ok=False`` + ``error_type``；
  上层按 error_type 重建对应异常 → HTTP 400 + 真因。
  刻意区分这两类：前者是「连不上」，后者是「连上了但被拒」，混淆会让前端把
  拒单显示成「券商不可用」。

★ INV-8（TD-25）：每个 transport 的 IO 都**必须带 timeout**，且 30s 是硬上界。
停机路径禁止无界等待 —— 曾出现 backup 无限重试把整个进程冻住的事故。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ★ 见 transport.py 头部「失败语义」：传输故障必须**真的**映射成
#   ``BrokerNotConnectedError``（503 + 「去连接券商」引导），否则那段契约只是注释。
#   这里用继承实现映射，而不是在每个调用点写 if/except —— 调用点有十几个
#   （routes / sync / order_watchdog / mcp / tools），漏一处就是一个静默 500。
from xtquant_client.base import BrokerNotConnectedError

from .ports import ConnectorError

#: 单次调用硬上界；低于此值的 timeout 由调用方指定，高于此值一律截断。
MAX_TIMEOUT = 30.0


class TransportError(ConnectorError, BrokerNotConnectedError):
    """传输层故障（不可达 / 超时 / 协议破损）→ HTTP 503 + 「券商连接」引导。

    双继承是**刻意的**，两侧语义都需要成立：
      * ``ConnectorError`` —— 调用方若按「连接器错误」统一捕获也能命中；
      * ``BrokerNotConnectedError`` —— ``signal_router._live`` 据此置
        ``broker_unavailable=True``，路由转 503 引导（而不是把「agent 没运行」
        误报成 400「请求非法」）。
    """


@dataclass(frozen=True)
class WireRequest:
    """方言无关的请求信封。

    ``op`` 是 **dialect op**（如 ``PLACE_ORDER`` / ``QUERY_POSITION``），由 Dialect 产出；
    Transport 只把它当作一个不透明字符串转发。
    """

    op: str
    params: dict[str, Any] = field(default_factory=dict)
    signal_id: str = ""
    timeout: float = MAX_TIMEOUT
    #: 是否为写操作。由连接器按**规范化 op** 判定（见 ``dialects.WRITE_OPS``），
    #: Transport 据此选择「串行写」还是「并发读」，**不再依赖方法名匹配**，
    #: 从而避免大小 QMT 方言动作名不同导致的串行化错位。
    write: bool = False

    def __post_init__(self) -> None:
        if not self.op:
            raise ValueError("wire request op is required")
        # INV-8：超时不得为负/为零/无界。
        object.__setattr__(self, "timeout", min(max(float(self.timeout), 0.1), MAX_TIMEOUT))
        if not self.signal_id:
            object.__setattr__(self, "signal_id", uuid.uuid4().hex[:16])


@dataclass(frozen=True)
class WireResponse:
    """统一响应信封。

    ★ ``ok`` 只能在**确实拿到了执行结果**时为 True。拿不到就走异常，
    绝不返回一个「空的 ok=True」（那正是零 mock 要防的假绿灯）。
    """

    ok: bool
    signal_id: str = ""
    result: Any = None
    error: str = ""
    error_type: str = ""
    elapsed_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, result: Any, *, signal_id: str = "",
                started: float = 0.0, **meta: Any) -> "WireResponse":
        return cls(
            ok=True,
            signal_id=signal_id,
            result=result,
            elapsed_ms=_elapsed_ms(started),
            meta=meta,
        )

    @classmethod
    def failure(cls, error: str, error_type: str = "BrokerError", *,
                signal_id: str = "", started: float = 0.0, **meta: Any) -> "WireResponse":
        return cls(
            ok=False,
            signal_id=signal_id,
            error=str(error)[:2000],
            error_type=error_type,
            elapsed_ms=_elapsed_ms(started),
            meta=meta,
        )

    def raise_for_status(self, exc_factory) -> Any:
        """ok=False 时按 error_type 抛异常，否则返回 result。"""
        if self.ok:
            return self.result
        raise exc_factory(self.error_type, self.error)


def _elapsed_ms(started: float) -> int:
    if not started:
        return 0
    return int((time.perf_counter() - started) * 1000)


@runtime_checkable
class Transport(Protocol):
    """传输层接口。实现方只需回答「怎么送到」，无需理解业务。"""

    transport_id: str

    async def invoke(self, request: WireRequest) -> WireResponse: ...

    async def close(self) -> None: ...


__all__ = [
    "MAX_TIMEOUT", "Transport", "TransportError", "WireRequest", "WireResponse",
]
