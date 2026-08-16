"""A 股交易规则（纯函数，零 mock）：整手、涨跌停价、T+1 可用量。

被回测撮合内核 (`tools/matching.py`)、模拟盘 (`paper/`)、下单风控 (`gateway/risk.py`)
复用，保证「回测/模拟盘/实盘」三套通道使用同一套 A 股规则，避免语义漂移。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ---------------- 板块涨跌停幅度 ----------------
def board_limit_pct(code: str, is_st: bool = False) -> float:
    """板块涨跌停幅度：

    - ST / *ST：5%
    - 创业板(30) / 科创板(68)：20%
    - 北交所(8/4/92)：30%
    - 其余（主板）：10%
    """
    c = (code or "").upper()
    if is_st:
        return 0.05
    if c.startswith("68") or c.startswith("30"):
        return 0.20
    if c.startswith("8") or c.startswith("4") or c.startswith("92"):
        return 0.30
    return 0.10


def limit_price(ref_close: float, direction: str,
                pct: float | None = None, code: str = "",
                is_st: bool = False) -> float:
    """计算涨跌停价（四舍五入到分）。

    ref_close 为昨收；direction 取 "up"/"buy" 算涨停、"down"/"sell" 算跌停。
    pct 缺省按板块/ST 推导。
    """
    if ref_close is None or ref_close <= 0:
        raise ValueError("ref_close 必须为正")
    if pct is None:
        pct = board_limit_pct(code, is_st)
    factor = (1 + pct) if str(direction).lower() in ("up", "buy", "limit_up") else (1 - pct)
    raw = ref_close * factor
    # 价格最小单位 0.01，四舍五入后防浮点误差
    return round(raw + 1e-9, 2)


def is_limit_up(code: str, last: float, ref_close: float,
                is_st: bool = False, tol: float = 1e-6) -> bool:
    """last 是否触涨停（含等于）。ref_close 缺失返回 False（无法判定）。"""
    if ref_close is None or ref_close <= 0 or last is None:
        return False
    return last >= limit_price(ref_close, "up", code=code, is_st=is_st) - tol


def is_limit_down(code: str, last: float, ref_close: float,
                  is_st: bool = False, tol: float = 1e-6) -> bool:
    """last 是否触跌停。"""
    if ref_close is None or ref_close <= 0 or last is None:
        return False
    return last <= limit_price(ref_close, "down", code=code, is_st=is_st) + tol


# ---------------- 整手 ----------------
def round_lot(qty: float, lot: int = 100) -> int:
    """向下取整到 lot 整数倍（A 股 100 股一手）。"""
    lot = lot or 100
    return int(math.floor(qty / lot) * lot)


def is_valid_lot(qty: int, lot: int = 100) -> bool:
    """数量是否为合法整手（正且为 lot 整数倍）。"""
    return qty is not None and qty > 0 and qty % lot == 0


# ---------------- T+1 可用量账本 ----------------
@dataclass
class T1Ledger:
    """T+1 可用数量跟踪。

    记录每笔买入的「到货序号」，序号达到当前索引即视为可卖。
    对日线 kline 而言，索引即自然日，买入当根 idx 的货在 idx+1（次根）可卖 = 真实的 T+1。
    对分钟线则是「T+1 根」，语义一致（滚动窗口内的次根可卖）。
    """

    lot: int = 100
    _lots: list = field(default_factory=list)  # 元素: [available_from_idx, qty]

    def buy(self, qty: int, idx: int) -> None:
        if qty and qty > 0:
            self._lots.append([idx + 1, int(qty)])

    def sellable(self, idx: int) -> int:
        return sum(q for (d, q) in self._lots if d <= idx)

    def consume(self, qty: int, idx: int) -> int:
        """从最旧批次开始扣减可卖量，返回实际扣减量。"""
        remaining = int(qty)
        consumed = 0
        for entry in self._lots:
            if remaining <= 0:
                break
            d, q = entry
            if d > idx:
                continue  # 尚未到货
            take = min(remaining, q)
            entry[1] = q - take
            remaining -= take
            consumed += take
        self._lots = [[d, q] for (d, q) in self._lots if q > 0]
        return consumed

    def position(self) -> int:
        return sum(q for (_, q) in self._lots)
