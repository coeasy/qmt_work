"""委托 / 成交的**字段契约归一**（券商原生名 → 平台契约名）。

为什么需要这一层
----------------
券商适配器（``xtquant_client/xtp/trading.py``）输出的是贴近 SDK 的原生名：

    orders → order_id / code / direction / price / volume / dealt / status
    deals  → order_id / code / direction / price / volume / time / seq

而界面与 REST 消费方历史上按另一套名字读：``side`` / ``filled`` / ``deal_id``。
两边各自演进，结果是**委托方向恒显示"卖出"、已成交恒 0、成交号空白，且不报错**——
比报错更危险：用户会照着错误的信息判断仓位。

更麻烦的是**后端自己也不统一**：``xtquant_client/order_status.py`` 定义标准状态为
``partial`` / ``cancelled``，但 ``gateway/reconcile.py`` 输出 ``part_filled``、
``engines/condition_order.py`` 用 ``canceled``。同一个"已撤"在系统里有两种写法。

因此这里做两件事：
1. **字段名归一**：补齐契约名，同时**保留原生名**（既有调用方与 MCP 工具按原生名读，
   不能破坏）；
2. **状态词表归一**：一律经 ``normalize_order_status`` 收敛到平台标准词表，
   原始值留存在 ``status_raw`` 供排障。

★ 原则：只**补**字段、不改语义；拿不到就留空，绝不臆造（与项目"零 mock"一致）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from xtquant_client.order_status import normalize_order_status

__all__ = [
    "normalize_order", "normalize_orders",
    "normalize_deal", "normalize_deals",
]

#: 契约名 ← 原生名（仅当契约名缺失时回填，已有一律不覆盖）
_ORDER_ALIASES = (
    ("side", ("direction",)),
    ("filled", ("dealt", "traded_volume", "deal_volume")),
    ("volume", ("order_volume", "ordered_volume")),
)

_DEAL_ALIASES = (
    ("side", ("direction",)),
    ("volume", ("traded_volume", "deal_volume")),
    ("price", ("traded_price", "deal_price")),
    ("time", ("traded_time", "deal_time")),
)


def _first(row: Dict[str, Any], keys: Iterable[str]) -> Optional[Any]:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _apply_aliases(row: Dict[str, Any], aliases) -> None:
    for contract, natives in aliases:
        if row.get(contract) in (None, ""):
            v = _first(row, natives)
            if v is not None:
                row[contract] = v


def normalize_order(row: Any) -> Any:
    """单条委托归一（原地补字段并返回）。

    - ``direction`` → 同时给 ``side``（值本身已是 buy/sell，无需翻译）；
    - ``dealt`` → 同时给 ``filled``；
    - ``status`` → 归一到标准词表（partial/cancelled/...），原始值留 ``status_raw``。
    """
    if not isinstance(row, dict):
        return row
    _apply_aliases(row, _ORDER_ALIASES)
    if "status" in row:
        raw = row.get("status")
        std = normalize_order_status(raw)
        # 只有真的变了才记 status_raw，避免每行都多一个冗余键
        if std != raw:
            row["status_raw"] = raw
        row["status"] = std
    return row


def normalize_orders(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    for r in rows:
        normalize_order(r)
    return rows


def normalize_deal(row: Any) -> Any:
    """单条成交归一。

    ★ ``deal_id``：真实成交回报**没有**独立的成交号，只有 ``order_id`` + ``seq``
    （旧 SDK 甚至只有 order_id）。界面用它做列表 key，必须有值且稳定 ——
    这里按 ``deal_id → order_id(+seq) → seq`` 逐级兜底，绝不返回空。
    """
    if not isinstance(row, dict):
        return row
    _apply_aliases(row, _DEAL_ALIASES)
    if not row.get("deal_id"):
        oid = row.get("order_id") or ""
        seq = row.get("seq")
        row["deal_id"] = f"{oid}#{seq}" if (oid and seq not in (None, "")) else (oid or str(seq or ""))
    return row


def normalize_deals(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    for r in rows:
        normalize_deal(r)
    return rows


def status_label(status: Optional[str]) -> str:
    """标准状态 → 中文文案（界面展示唯一入口，避免各处硬编码）。"""
    return {
        "pending": "待成交",
        "partial": "部分成交",
        "filled": "已成交",
        "cancelled": "已撤单",
        "rejected": "废单",
        "unknown": "未知",
    }.get(str(status or ""), str(status or "--"))
