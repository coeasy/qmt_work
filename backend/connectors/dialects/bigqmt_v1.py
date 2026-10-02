"""大 QMT（完整版）方言：passorder / cancel / get_trade_detail_data / ContextInfo。

适用形态：「内置策略桥接」（V4 §3.1 的 ``qmt.big.bridge.*``）。
另一种形态 ``qmt.big.direct``（xtquant 直连）复用 ``XtQuantV1``，**不在这里**。

wire op 是大 QMT agent 的 action 词表，属于**本方言内部格式**，不对外暴露到
REST/WS 契约面（V4 §9 第 3 条修正）。

★ py3.6 兼容纪律只约束 ``agent_bigqmt/`` （真正跑在大 QMT 内置 Python 里的那部分）。
  本文件运行在 qmt_work 后端（py3.11+），但刻意**只用标准库 + typing**，
  以便未来需要时能整段搬进 agent 侧。
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

# canonical op → 大 QMT agent action
_OP_TO_ACTION = {
    Ops.PLACE_ORDER: "PLACE",          # 由 payload.side 决定买/卖（见 prepare）
    Ops.CANCEL_ORDER: "CANCEL_ORDER",
    Ops.GET_ORDERS: "QUERY_ORDER",
    Ops.GET_DEALS: "QUERY_TRADE",
    Ops.GET_ACCOUNT: "QUERY_ASSET",
    Ops.GET_CASH: "QUERY_ASSET",
    Ops.GET_POSITIONS: "QUERY_POSITION",
    Ops.GET_QUOTE: "QUERY_QUOTE",
    Ops.GET_KLINE: "QUERY_KLINE",
    Ops.GET_FULL_TICK: "QUERY_QUOTE",
    Ops.SUBSCRIBE_QUOTE: "SUB_QUOTE",
    Ops.GET_STOCK_LIST: "QUERY_STOCK_LIST",
    Ops.GET_SECTOR_LIST: "QUERY_SECTOR_LIST",
    Ops.GET_INSTRUMENT_DETAIL: "QUERY_INSTRUMENT",
    Ops.GET_TRADING_CALENDAR: "QUERY_CALENDAR",
    Ops.TEST_CONNECTION: "PROBE",
    Ops.PROBE: "PROBE",
}

# 大 QMT 原始字段名 → canonical（agent 侧已做一层映射，这里是兜底兼容：
# 不同客户端版本返回的键名不完全一致）
_ASSET_KEYS = {
    "account_id": ("account_id", "accountId", "m_strAccountID"),
    # ★ ``m_dBalance`` **绝不能**进 cash 候选：在 QMT 里它是总资产（m_dBalance），
    #   一旦某券商版本只返回 m_dBalance 而不返回 m_dAvailable，会把「总资产」
    #   误当成「可用资金」→ 风控用膨胀的可用资金放行真单。可用资金只用
    #   m_dAvailable / available。
    "cash": ("cash", "m_dAvailable", "available"),
    "frozen": ("frozen", "m_dFrozenCash", "frozen_cash"),
    "assets": ("assets", "total_asset", "m_dBalance", "m_dAssetBalance"),
}
_POSITION_KEYS = {
    "code": ("code", "stock_code", "m_strInstrumentID"),
    "name": ("name", "m_strInstrumentName"),
    "volume": ("volume", "m_nVolume"),
    "avail": ("avail", "can_use_volume", "m_nCanUseVolume"),
    "cost": ("cost", "open_price", "m_dOpenPrice"),
    "market_value": ("market_value", "m_dMarketValue"),
}


class BigQmtV1:
    """大 QMT 内置 API 方言。"""

    dialect_id = "bigqmt.v1"

    #: 共享 xtquant 的状态 SSOT —— 两者整数同源，禁止在此另建映射表（INV-3）。
    status_ssot = "xtquant_client.order_status"

    #: 写操作（**方言动作名**）。本方言走 file/redis/zmq 传输，串行化由传输层
    #: 按规范化 op 判定；这里声明它是为了与 xtquant.v1 / ptrade.v1 保持同形
    #: （诊断与能力面据此可知「哪些动作会改柜台状态」）。
    write_ops = frozenset({"PLACE", "CANCEL_ORDER", "SUB_QUOTE"})

    def supported_ops(self) -> tuple[str, ...]:
        """本方言能翻译的 canonical op 清单（由映射表派生，绝不手抄）。"""
        return tuple(_OP_TO_ACTION)

    def op_of(self, op: str) -> str:
        try:
            return _OP_TO_ACTION[op]
        except KeyError as exc:
            raise UnsupportedOp(
                f"大 QMT 方言不支持操作: {op}"
                f"（支持: {', '.join(sorted(set(_OP_TO_ACTION.values())))}）") from exc

    # ------------------------------------------------------------------
    def prepare(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op == Ops.PLACE_ORDER:
            side = str(payload.get("side", "")).lower()
            # 未给出方向就下单是**危险**的：宁可报错也不猜（买/卖代价天差地别）。
            if side not in ("buy", "sell"):
                raise ValueError(f"PLACE 需要明确的 side(buy/sell)，收到: {side!r}")
            return {
                "stock_code": payload.get("code"),
                "side": side,
                "price_type": payload.get("order_type", "limit"),
                "price": payload.get("price", 0.0),
                "volume": payload.get("quantity", 0),
                "account_id": payload.get("account_id", ""),
                "strategy_name": payload.get("strategy_name", ""),
                "remark": payload.get("remark", ""),
                "client_order_id": payload.get("client_order_id", ""),
                # 下单级账户/标的类型（stock/etf/future/option/credit）→ agent 侧
                # ``Executor.do_place`` 解析成 passorder 的 ``opAccountType``。
                # 空串 = 不覆盖（由 agent 的 default_account_type / stock 兜底）。
                "account_type": payload.get("account_type", "") or "",
                # D3：幽灵单防护的时间预算。缺省 0=不过期（向后兼容）。
                # ★ 白名单式产出（D7 / easytrader #520 教训）：调用方多传的
                #   未知键必须在这里被丢弃，绝不能透传到 agent 的严格签名。
                "ttl_ms": int(payload.get("ttl_ms") or 0),
            }
        if op == Ops.CANCEL_ORDER:
            return {"order_id": payload.get("order_id", ""),
                    "account_id": payload.get("account_id", ""),
                    "ttl_ms": int(payload.get("ttl_ms") or 0)}
        if op == Ops.SUBSCRIBE_QUOTE:
            # on_tick 回调**不可过线**（进程内对象）；订阅语义 = agent 侧
            # 轮询转发 quote 事件进 events.ndjson（见 qmt_api.do_subscribe）。
            return {"codes": [c for c in (payload.get("codes") or []) if c]}
        if op == Ops.GET_STOCK_LIST:
            return {"sector": payload.get("sector") or "沪深A股",
                    "limit": int(payload.get("limit") or 0)}
        if op == Ops.GET_INSTRUMENT_DETAIL:
            return {"code": payload.get("code")}
        if op == Ops.GET_POSITIONS:
            return {"symbol": payload.get("symbol", ""),
                    "account_id": payload.get("account_id", "")}
        if op == Ops.GET_KLINE:
            return {
                "stock_code": payload.get("code"),
                "period": payload.get("period"),
                "count": payload.get("count", 0),
                "start": payload.get("start", ""),
                "end": payload.get("end", ""),
                "adjust": payload.get("adjust", ""),
            }
        if op in (Ops.GET_QUOTE, Ops.GET_FULL_TICK):
            codes = payload.get("codes") or [payload.get("code")]
            return {"codes": [c for c in codes if c]}
        return dict(payload or {})

    # ------------------------------------------------------------------
    def parse(self, op: str, result: Any) -> Any:
        if result is None:
            return None
        if op == Ops.PLACE_ORDER:
            return order_snapshot(_remap(result, {}),
                                  client_order_id=str(result.get("client_order_id") or ""))
        if op == Ops.CANCEL_ORDER:
            return order_snapshot(result)
        if op == Ops.GET_ORDERS:
            return [order_snapshot(r) for r in (result or [])]
        if op == Ops.GET_DEALS:
            return [trade_snapshot(r) for r in (result or [])]
        if op == Ops.GET_POSITIONS:
            rows = [_remap(r, _POSITION_KEYS) for r in (result or [])]
            return [position_snapshot(r) for r in rows]
        if op in (Ops.GET_ACCOUNT, Ops.GET_CASH):
            # get_trade_detail_data("ACCOUNT") 返回列表；QUERY_ASSET 也可能直接给 dict。
            src = result[0] if isinstance(result, list) and result else result
            return account_snapshot(_remap(src or {}, _ASSET_KEYS))
        return result

    # ------------------------------------------------------------------
    def classify_error(self, exc: BaseException) -> tuple[str, str]:
        name = type(exc).__name__
        msg = str(exc)
        if name == "BrokerNotConnectedError":
            return "BrokerNotConnected", msg
        if "inject" in msg.lower() or "缺失" in msg:
            return "BrokerSDKError", msg
        if isinstance(exc, TimeoutError):
            return "Timeout", msg
        return "BrokerError", msg


def _remap(raw: Any, mapping: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """按候选键名把方言字段映射成 canonical 键名；未命中的原样保留到 raw。

    不做**类型转换**：让 canonicalize 统一处理（那里有统一的容错策略）。
    """
    if not isinstance(raw, dict):
        return {}
    if not mapping:
        return dict(raw)
    out = dict(raw)
    for canonical_key, candidates in mapping.items():
        if canonical_key in out:
            continue
        for cand in candidates:
            if cand in raw and raw[cand] is not None:
                out[canonical_key] = raw[cand]
                break
    return out


__all__ = ["BigQmtV1"]
