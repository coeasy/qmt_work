"""方言 raw dict → canonical 快照的唯一转换层（P0 阶段新增）。

为什么单独一个文件：
``connectors/ports.py`` 只承载「契约」（Protocol + frozen dataclass），
方言侧的字段提取属于「实现」，两者混在一起会让契约层长出 SDK 知识。

三条铁律（改动前请先看 ``docs/archive/UNIFIED_TRADING_ABSTRACTION.md`` §5.2）：
1. **状态一律过 ``order_status`` SSOT**：大 QMT 的 ``m_nOrderStatus`` 与 xtquant
   的 ``OrderStatus`` 是同一整数族群（50 已报 / 56 已成 / 57 废单），因此这里
   **不再建第二张映射表**；方言原始值只落到 ``raw_status`` 供诊断。
2. **不臆造**：缺字段就留 ``UNKNOWN`` / 0 / ``False``。拿不到柜台委托号时
   ``accepted`` 必须为 ``False`` ——把「未确认」粉饰成「已受理」正是线上事故
   的常见来源（SignalRouter 依赖它决定 ok 还是 503）。
3. **字段名多版本兼容**：旧版 xtquant 全小写、新版部分 CamelCase；大 QMT
    ``get_trade_detail_data`` 返回的又是另一批名字。统一在这里兜住，上层只见 canonical 键。
"""
from __future__ import annotations

from typing import Any, Mapping

from xtquant_client.order_status import (
    REJECTED,
    UNKNOWN,
    normalize_order_status,
)

from .ports import (
    AccountSnapshot,
    InstrumentId,
    OrderSnapshot,
    PositionSnapshot,
)


def _take(d: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """按候选键名顺序取值；全命中不到返回 default。

    用显式候选名单而非 ``**kwargs``：键名漂移是这里唯一要解决的问题，
    引入可变参数会让调用方以为可以传任意东西。
    """
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _int(d: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    v = _take(d, *keys)
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(d: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    v = _take(d, *keys)
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _str(d: Mapping[str, Any], *keys: str, default: str = "") -> str:
    v = _take(d, *keys)
    return default if v is None else str(v)


def _split_code(code: str) -> InstrumentId:
    """``600036.SH`` → InstrumentId(code=600036, exchange=SH)。

    无交易所后缀时不猜（返回空 exchange）：猜测会让 ``canonical`` 拼出一个
    看似合法实则错误的代码，比留空更难排查。
    """
    code = (code or "").strip()
    if "." in code:
        base, _, ex = code.partition(".")
        return InstrumentId(code=base, exchange=ex.upper())
    return InstrumentId(code=code)


def _accepted(status: str, broker_order_id: str) -> bool:
    """柜台是否受理。

    判据必须同时满足两条，缺一不可：
      - 有柜台委托号（``order_id``/``order_sysid``）——没有号码谈不上受理；
      - 状态已由 SSOT 判定出来且不是 REJECTED——废单虽然可能有号码，
        但「已受理并成交/挂单」的语义不成立。
    """
    if not broker_order_id:
        return False
    if status in (UNKNOWN, REJECTED):
        return False
    return True


def order_snapshot(raw: Mapping[str, Any], *, client_order_id: str = "") -> OrderSnapshot:
    """当日委托 / 下单结果 → canonical 快照。

    兼容的 raw 形态：
      - xtp ``get_orders``：order_id/code/direction/price/volume/dealt/status
      - xtp ``place_order``：order_id/status/...（可能只有 order_id）
      - easytrader 风格：order_id/order_sysid/stock_code/order_status
    """
    if not isinstance(raw, Mapping):
        # 零 mock：宁可返回一个 status=UNKNOWN 的诚实快照，也不伪造字典内容。
        raw = {}
    raw_status = _take(raw, "status", "order_status", "order_status_name", default="")
    status = normalize_order_status(raw_status) if raw_status not in ("", None) else UNKNOWN
    oid = _str(raw, "order_id", "order_sysid")
    return OrderSnapshot(
        status=status,
        broker_order_id=oid,
        client_order_id=client_order_id or _str(raw, "client_order_id", "remark"),
        accepted=_accepted(status, oid),
        instrument=_split_code(_str(raw, "code", "stock_code")),
        side=_str(raw, "direction").lower(),
        requested_quantity=_int(raw, "volume", "order_volume", "ordered_volume"),
        filled_quantity=_int(raw, "dealt", "traded_volume", "deal_volume"),
        avg_price=_float(raw, "traded_price", "deal_price"),
        raw_status=str(raw_status),
        raw=dict(raw),
    )


def trade_snapshot(raw: Mapping[str, Any], *, client_order_id: str = "") -> OrderSnapshot:
    """当日成交 → canonical 快照。

    成交行本身即「已发生成交」的证据，故 status 填 FILLED 不算臆造
    （区别于委托：委托缺状态时只能是 UNKNOWN）。
    """
    from xtquant_client.order_status import FILLED

    if not isinstance(raw, Mapping):
        raw = {}
    vol = _int(raw, "volume", "traded_volume", "deal_volume")
    return OrderSnapshot(
        status=FILLED,
        broker_order_id=_str(raw, "order_id", "order_sysid"),
        client_order_id=client_order_id or _str(raw, "client_order_id"),
        accepted=True,
        instrument=_split_code(_str(raw, "code", "stock_code")),
        side=_str(raw, "direction").lower(),
        requested_quantity=vol,
        filled_quantity=vol,
        avg_price=_float(raw, "price", "traded_price", "deal_price"),
        raw_status="",
        raw=dict(raw),
    )


def position_snapshot(raw: Mapping[str, Any]) -> PositionSnapshot:
    """持仓 raw dict → canonical 快照（code/name/volume/avail/cost/market_value）。

    ★ ``raw=dict(raw)`` 必须保留（曾漏掉）：大 QMT 桥的 ``_raw_of()`` 与旧适配器面
      都直接消费 ``snap.raw`` 的 dict 键（``code`` / ``name`` / ``avail`` …）。
      漏掉后持仓行会退化成 ``{}``，且**不抛异常** —— 账户看板显示空仓、
      ``enrich_positions`` 拿不到代码，排查时看不到任何报错。
    """
    if not isinstance(raw, Mapping):
        raw = {}
    return PositionSnapshot(
        instrument=_split_code(_str(raw, "code", "stock_code")),
        quantity=_int(raw, "volume", "can_use_volume"),
        available_quantity=_int(raw, "avail", "can_use_volume"),
        average_cost=_float(raw, "cost", "open_price", "avg_price"),
        market_value=_float(raw, "market_value"),
        name=_str(raw, "name", "instrument_name"),
        raw=dict(raw),
    )


def account_snapshot(raw: Mapping[str, Any]) -> AccountSnapshot:
    """账户 raw dict → canonical 快照。

    ``raw`` 保留全量原始字段：各家券商资金字段口径不一，排查 cash/assets 对不上时
    上层需要回看原始值，标准字段覆盖不了全部场景。
    """
    if not isinstance(raw, Mapping):
        raw = {}
    return AccountSnapshot(
        account_id=_str(raw, "account_id"),
        cash=_float(raw, "cash"),
        frozen=_float(raw, "frozen", "frozen_cash"),
        assets=_float(raw, "assets", "total_asset"),
        raw=dict(raw),
    )


__all__ = [
    "account_snapshot",
    "order_snapshot",
    "position_snapshot",
    "trade_snapshot",
]
