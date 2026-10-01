"""Ptrade（恒生 PTrade）方言 —— **接入模板**（V4 Phase 4-a）。

存在的意义：证明「新增一个客户端只需新增一个 dialect」这条承诺是真的。
本文件就是那第 3 个 dialect，它与 ``xtquant.v1``、``bigqmt.v1`` 完全对称：

    registry.resolve("ptrade.http", base_url=..., api_key=...)

★ 诚实标注（重要）：字段名取的是 PTrade REST 网关的**常见约定**，
  **未经实机联调验证**。因此接入时必须：
   1. 先用 PROBE 打通连通性与 op 清单；
   2. 逐条核对 ``prepare/parse`` 的字段口径，改这个文件即可，
      **编排层、传输层、契约层都不用动**；
   3. 未验证的能力在 probe 里保持 UNKNOWN（不要手写成 SUPPORTED）。
"""
from __future__ import annotations

from typing import Any

from ..canonicalize import account_snapshot, order_snapshot, position_snapshot
from .base import Ops, UnsupportedOp

_OP_TO_CMD = {
    Ops.PLACE_ORDER: "place_order",
    Ops.CANCEL_ORDER: "cancel_order",
    Ops.GET_ORDERS: "get_orders",
    Ops.GET_DEALS: "get_trades",
    Ops.GET_ACCOUNT: "get_account",
    Ops.GET_CASH: "get_cash",
    Ops.GET_POSITIONS: "get_positions",
    Ops.GET_QUOTE: "get_quote",
    Ops.GET_KLINE: "get_kline",
    Ops.GET_STOCK_LIST: "get_stock_list",
    Ops.GET_TRADING_CALENDAR: "get_trading_calendar",
    Ops.TEST_CONNECTION: "ping",
    Ops.PROBE: "probe",
}

_WRITE = frozenset({"place_order", "cancel_order"})


class PtradeV1:
    dialect_id = "ptrade.v1"

    #: 复用电报谱 SSOT：待实机确认其整数族群是否同源；**不同源则在此注释里
    #: 指出差异并在 canonicalize 里加一侧分支**，而不是新建第 N 张映射表。
    status_ssot = "xtquant_client.order_status"

    write_ops = _WRITE

    def supported_ops(self) -> tuple[str, ...]:
        """本方言能翻译的 canonical op 清单（由映射表派生，绝不手抄）。"""
        return tuple(_OP_TO_CMD)

    def op_of(self, op: str) -> str:
        try:
            return _OP_TO_CMD[op]
        except KeyError as exc:
            raise UnsupportedOp(f"ptrade dialect 不支持操作: {op}") from exc

    def prepare(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op == Ops.PLACE_ORDER:
            return {
                "symbol": payload.get("code"),
                "side": payload.get("side"),
                "type": payload.get("order_type", "limit"),
                "price": payload.get("price", 0.0),
                "amount": payload.get("quantity", 0),
                "client_order_id": payload.get("client_order_id", ""),
            }
        if op == Ops.CANCEL_ORDER:
            return {"order_id": payload.get("order_id", "")}
        if op == Ops.GET_POSITIONS:
            return {"symbol": payload.get("symbol") or ""}
        if op == Ops.GET_KLINE:
            return {"symbol": payload.get("code"), "period": payload.get("period"),
                    "count": payload.get("count", 0), "start": payload.get("start", ""),
                    "end": payload.get("end", "")}
        return dict(payload or {})

    def parse(self, op: str, result: Any) -> Any:
        if result is None:
            return None
        if op == Ops.PLACE_ORDER:
            return order_snapshot(result, client_order_id=str(result.get("client_order_id") or ""))
        if op == Ops.CANCEL_ORDER:
            return order_snapshot(result)
        if op == Ops.GET_ORDERS:
            return [order_snapshot(r) for r in (result or [])]
        if op == Ops.GET_DEALS:
            from ..canonicalize import trade_snapshot
            return [trade_snapshot(r) for r in (result or [])]
        if op == Ops.GET_POSITIONS:
            return [position_snapshot(r) for r in (result or [])]
        if op in (Ops.GET_ACCOUNT, Ops.GET_CASH):
            return account_snapshot(result if isinstance(result, dict) else {})
        return result

    def classify_error(self, exc: BaseException) -> tuple[str, str]:
        name = type(exc).__name__
        msg = str(exc)
        if name == "BrokerNotConnectedError":
            return "BrokerNotConnected", msg
        if isinstance(exc, TimeoutError):
            return "Timeout", msg
        return "BrokerError", msg


__all__ = ["PtradeV1"]
