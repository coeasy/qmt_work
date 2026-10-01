"""Dialect 抽象：只管「说什么」，不知道自己在哪个进程（V4 §3）。"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..ports import ConnectorError


class Ops:
    """canonical 操作码（端口/方言共用的唯一词表）。

    dialect.op_of(op) 把它翻译成方言自己的指令名；Transport 把它当不透明字符串。
    """

    PLACE_ORDER = "PLACE_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    GET_ORDERS = "GET_ORDERS"
    GET_DEALS = "GET_DEALS"
    GET_ACCOUNT = "GET_ACCOUNT"
    GET_CASH = "GET_CASH"
    GET_POSITIONS = "GET_POSITIONS"
    GET_QUOTE = "GET_QUOTE"
    GET_KLINE = "GET_KLINE"
    GET_FULL_TICK = "GET_FULL_TICK"
    SUBSCRIBE_QUOTE = "SUBSCRIBE_QUOTE"
    GET_INSTRUMENT_DETAIL = "GET_INSTRUMENT_DETAIL"
    GET_STOCK_LIST = "GET_STOCK_LIST"
    GET_SECTOR_LIST = "GET_SECTOR_LIST"
    GET_TRADING_CALENDAR = "GET_TRADING_CALENDAR"
    TEST_CONNECTION = "TEST_CONNECTION"
    PROBE = "PROBE"          # 握手 / 能力探测（大 QMT 桥接必须支持）


#: 写操作集合：必须串行化，且走 ``call_locked``（Transport 决定是否使用）。
WRITE_OPS = frozenset({Ops.PLACE_ORDER, Ops.CANCEL_ORDER, Ops.SUBSCRIBE_QUOTE})


def is_write_op(op: str) -> bool:
    """规范化 op 是否为写操作（供连接器设置 ``WireRequest.write``）。

    这是**唯一**的写操作判定来源：Transport 据此选串行/并发，
    不再依赖「adapter 方法名是否在白名单」的脆弱匹配 —— 大小 QMT 方言的
    动作名（``place_order`` vs ``PLACE``）本就不同，靠方法名会错位。
    """
    return op in WRITE_OPS


@runtime_checkable
class Dialect(Protocol):
    """方言：canonical 概念 ↔ 方言指令的双向翻译器。

    ★ 成员的**存在性**是对称性要求，不是装饰：大小 QMT 的动作名本就不同
      （``place_order`` vs ``PLACE``），只要有一个方言漏声明 ``write_ops`` /
      ``supported_ops``，调用方就只能靠 ``getattr(..., default)`` 兜底 ——
      而兜底出来的默认值会**伪装成正确**，让「这个方言到底声明了什么」
      不可观测。三个内置方言（xtquant.v1 / bigqmt.v1 / ptrade.v1）全部实现。
    """

    dialect_id: str

    #: 写操作（**方言指令名**）。Transport 据此选串行/并发。
    write_ops: frozenset

    def op_of(self, op: str) -> str:
        """canonical op → 方言指令名。"""

    def supported_ops(self) -> tuple:
        """本方言能翻译的 canonical op 全量清单（由 op 映射表派生，不手抄）。"""

    def prepare(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """canonical 参数 → 方言 wire 参数。"""

    def parse(self, op: str, result: Any) -> Any:
        """方言返回 → canonical 模型（行情类数据面保持原样 dict）。"""

    def classify_error(self, exc: BaseException) -> tuple[str, str]:
        """异常 → ``(error_type, message)``。用于跨进程/跨传输的异常重建。"""


class UnsupportedOp(ConnectorError):
    """该方言不支持该操作（区别于「调用失败」：这是能力问题，应在能力面声明）。

    ★ 继承 ``ConnectorError``（→ ``BrokerError``）而非 ``RuntimeError``：本异常会
      从 ``GenericConnector.subscribe_quote`` 等真实业务路径冒到路由层，独立于
      券商异常层级时会被 FastAPI 兜底成 500，把「这个传输不支持回调订阅」
      这种**可引导**的能力缺口伪装成服务器故障。
    """


__all__ = ["Dialect", "Ops", "UnsupportedOp", "WRITE_OPS"]
