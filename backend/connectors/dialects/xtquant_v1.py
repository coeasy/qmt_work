"""XtQuant 方言：mini QMT 与「xtquant 直连大 QMT」共用（V4 §3.1）。

这一层是从 ``XTPQuantAdapter`` 上剥离出来的**纯翻译**，不含任何调用/传输逻辑：
同一份方言既能配 InProcess（进程内 SDK），也能配 SubprocessBridge（ABI 不匹配时的
子进程桥）——二者差异在 Transport，不在这里。
"""
from __future__ import annotations

from typing import Any

from ..canonicalize import (
    account_snapshot,
    order_snapshot,
    position_snapshot,
    trade_snapshot,
)
from .base import Ops, UnsupportedOp

# canonical op → xtquant adapter 方法名
_OP_TO_ADAPTER = {
    Ops.PLACE_ORDER: "place_order",
    Ops.CANCEL_ORDER: "cancel_order",
    Ops.GET_ORDERS: "get_orders",
    Ops.GET_DEALS: "get_deals",
    Ops.GET_ACCOUNT: "get_account",
    Ops.GET_CASH: "get_cash",
    Ops.GET_POSITIONS: "get_positions",
    Ops.GET_QUOTE: "get_quote",
    Ops.GET_KLINE: "get_kline",
    Ops.GET_FULL_TICK: "get_full_tick",
    Ops.SUBSCRIBE_QUOTE: "subscribe_quote",
    Ops.GET_INSTRUMENT_DETAIL: "get_instrument_detail",
    Ops.GET_STOCK_LIST: "get_stock_list",
    Ops.GET_SECTOR_LIST: "get_sector_list",
    Ops.GET_TRADING_CALENDAR: "get_trading_calendar",
    Ops.TEST_CONNECTION: "test_connection",
    Ops.PROBE: "test_connection",       # 进程内心跳即探活
}


class XtQuantV1:
    """XtQuant SDK 方言（``OrderStock`` / ``query_stock_asset`` …动词族）。"""

    dialect_id = "xtquant.v1"

    #: 状态整数族群与大 QMT 的 m_nOrderStatus 同源，故无需自有映射表（INV-3）。
    status_ssot = "xtquant_client.order_status"

    #: 写操作（**方言指令名**）。★ 三个方言必须都有这一项：
    #: 缺失时 `execution._port_for_bridge` 会静默退到 Transport 的默认白名单，
    #: 「有没有显式声明」就变得不可观测 —— 大 QMT 的动作名（PLACE）与 xtquant 的
    #: 方法名（place_order）本就不同，靠默认值兜底是脆弱的。
    write_ops = frozenset({
        "place_order", "cancel_order", "subscribe_quote", "cancel_order_price",
    })

    def supported_ops(self) -> tuple[str, ...]:
        """本方言能翻译的 canonical op 清单（由映射表派生，绝不手抄）。"""
        return tuple(_OP_TO_ADAPTER)

    def op_of(self, op: str) -> str:
        try:
            return _OP_TO_ADAPTER[op]
        except KeyError as exc:
            raise UnsupportedOp(f"xtquant dialect 不支持操作: {op}") from exc

    # ------------------------------------------------------------------
    def prepare(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """canonical 参数 → adapter 位置/关键字参数字典。

        只做**改名与补齐**，不做业务判断：价格合法性由 ``OrderRequest.validate()``
        在更上游处理。
        """
        if op == Ops.PLACE_ORDER:
            return {
                "code": payload.get("code"),
                "direction": payload.get("side"),
                "price_type": payload.get("order_type"),
                "price": payload.get("price", 0.0),
                "volume": payload.get("quantity", 0),
                "strategy_name": payload.get("strategy_name", ""),
                "remark": payload.get("remark", ""),
            }
        if op == Ops.GET_POSITIONS:
            return {"symbol": payload.get("symbol")}
        if op == Ops.GET_KLINE:
            return {
                "code": payload.get("code"),
                "period": payload.get("period"),
                "count": payload.get("count", 0),
                "start": payload.get("start", ""),
                "end": payload.get("end", ""),
                "adjust": payload.get("adjust", "") or None,
            }
        if op == Ops.GET_TRADING_CALENDAR:
            return {"start": payload.get("start", ""), "end": payload.get("end", "")}
        # 其余操作的 canonical 参数名与 adapter 形参同名，原样透传。
        return dict(payload or {})

    # ------------------------------------------------------------------
    def parse(self, op: str, result: Any) -> Any:
        """方言返回 → canonical 快照（行情类保持 dict/list[dict]）。"""
        if result is None:
            return None
        if op == Ops.PLACE_ORDER:
            return order_snapshot(result, client_order_id=_extract_cid(result))
        if op == Ops.CANCEL_ORDER:
            return order_snapshot(result)
        if op == Ops.GET_ORDERS:
            return [order_snapshot(r) for r in (result or [])]
        if op == Ops.GET_DEALS:
            return [trade_snapshot(r) for r in (result or [])]
        if op == Ops.GET_POSITIONS:
            return [position_snapshot(r) for r in (result or [])]
        if op in (Ops.GET_ACCOUNT, Ops.GET_CASH):
            return account_snapshot(result)
        # 行情/合约/日历：保持原生结构（数据面负责其字段契约）
        return result

    # ------------------------------------------------------------------
    def classify_error(self, exc: BaseException) -> tuple[str, str]:
        """把 xtquant 侧异常归到 wire 层的 error_type（端点据此重建异常 → 400/503）。"""
        name = type(exc).__name__
        msg = str(exc)
        if name == "BrokerNotConnectedError":
            return "BrokerNotConnected", msg
        if name == "BrokerSDKError":
            return "BrokerSDKError", msg
        if name == "BrokerError":
            return "BrokerError", msg
        if isinstance(exc, TimeoutError):
            return "Timeout", msg
        return "ConnectorError", f"{name}: {msg}"


def _extract_cid(raw: Any) -> str:
    """从柜台回执的 remark 里还原 client_order_id（P0-e 的回读侧）。

    拿不到就返回空串 —— 幂等锚点缺失是**可观测事件**，不能被伪造成有效值。
    """
    remark = ""
    if isinstance(raw, dict):
        remark = str(raw.get("remark") or "")
    if "cid=" not in remark:
        return ""
    tail = remark.split("cid=", 1)[1].split("|", 1)[0]
    return tail.strip()


__all__ = ["XtQuantV1"]
