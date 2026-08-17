"""订单状态统一词汇表（阶段 0-A 核心）。

根因（方案 F12 / 0-A·3）：`reconcile._norm_status`、`order_watchdog._ACTIVE`、`sync._order_fp`
三处各自维护一套状态映射，xtp 实际返回 ``fully_dealt`` / ``part_deal`` / ``unreported`` 等
原始串落入「未知分支」，导致对真实券商永远对不准账、活跃/部成单被误终态化。

本模块把「券商原始状态 → 平台标准状态」收敛为**唯一真相来源**，所有消费方必须引用此处，
不得再各自硬编码映射。平台标准状态：

- ``pending``   未报/待报/已报/提交中（仍未成交，可被超时守护撤单）
- ``partial``   部分成交（仍挂单，可被超时守护撤单）
- ``filled``    全部成交（终态）
- ``cancelled`` 已撤（终态）
- ``rejected``  废单/已拒绝（终态）
- ``unknown``   未知（无法判定，绝不能静默映射成其它状态）
"""
from __future__ import annotations

# 平台标准状态常量
PENDING = "pending"
PARTIAL = "partial"
FILLED = "filled"
CANCELLED = "cancelled"
REJECTED = "rejected"
UNKNOWN = "unknown"

# xtp 整数状态码（xtquant `XtOrderResponse.OrderStatus`）→ 标准状态
_XTP_INT_STATUS = {
    48: PENDING,    # 未报
    49: PENDING,    # 待报
    50: PENDING,    # 已报
    51: PENDING,    # 已报待撤
    52: PARTIAL,    # 部成待撤
    53: PARTIAL,    # 部成
    54: CANCELLED,  # 已撤
    55: PARTIAL,    # 部成（部分成交）
    56: FILLED,     # 全部成交
    57: REJECTED,   # 废单
    86: PENDING,    # 已确认（待成交）
    255: UNKNOWN,   # 未知
}

# 券商原始状态字符串（中英混排，含 xtp 实际返回的小写英文）→ 标准状态
_RAW_TO_STD = {
    # —— 中文 ——
    "未报": PENDING, "待报": PENDING, "已报": PENDING, "已报待撤": PENDING,
    "部成待撤": PARTIAL, "部分成交": PARTIAL, "部成": PARTIAL,
    "已撤": CANCELLED, "已撤单": CANCELLED, "撤单": CANCELLED,
    "已成": FILLED, "全部成交": FILLED,
    "废单": REJECTED, "已拒绝": REJECTED,
    # —— xtp 实际返回的小写英文（方案 F12 点名漏覆盖的）——
    "unreported": PENDING, "wait_report": PENDING, "reported": PENDING,
    "submitted": PENDING, "pending": PENDING, "queued": PENDING,
    "active": PENDING, "not_dealt": PENDING, "submitting": PENDING,
    "reported_cancel_pending": PENDING, "part_deal_cancel_pending": PARTIAL,
    "part_cancel": PARTIAL, "part_deal": PARTIAL, "part_filled": PARTIAL,
    "partial": PARTIAL,
    "fully_dealt": FILLED, "filled": FILLED,
    "canceled": CANCELLED, "cancelled": CANCELLED,
    "rejected": REJECTED, "junk": REJECTED,
    "unknown": UNKNOWN, "confirmed": PENDING,
}

# 终态集合（成交/撤单/废单后不可再回退）
_TERMINAL = frozenset({FILLED, CANCELLED, REJECTED})
# 活跃集合（仍未成交，超时守护可撤单）
_OPEN = frozenset({PENDING, PARTIAL})


def normalize_order_status(raw) -> str:
    """把券商任意形态的原始状态归一化为平台标准状态。

    - 接受 int（xtp 数值码）、数值字符串、中/英文字符串；
    - 未知形态**显式返回 ``unknown``**，绝不静默映射成 pending/filled 等。
    """
    if raw is None:
        return UNKNOWN
    # 数值码（xtp int 状态）
    try:
        iv = int(raw)
        if iv in _XTP_INT_STATUS:
            return _XTP_INT_STATUS[iv]
    except (ValueError, TypeError):
        pass
    s = str(raw).strip()
    if not s:
        return UNKNOWN
    low = s.lower()
    if s in _RAW_TO_STD:
        return _RAW_TO_STD[s]
    if low in _RAW_TO_STD:
        return _RAW_TO_STD[low]
    return UNKNOWN


def is_terminal(status) -> bool:
    """是否为终态（成交/撤单/废单后不应再回退）。"""
    return normalize_order_status(status) in _TERMINAL


def is_active(status) -> bool:
    """是否仍为活跃挂单（未成交或部分成交，可被超时守护撤单）。"""
    return normalize_order_status(status) in _OPEN
