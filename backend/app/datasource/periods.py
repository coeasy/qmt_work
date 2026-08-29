"""K 线周期契约：单点定义（Single Source of Truth）。

背景（2026-08-29 P0-1 静默错误）：
    周期枚举曾在 4 处各自硬编码（前端 PERIODS / eltdx_source._map_period /
    registry.adj_periods / market.py 同步任务默认值），且后端对未知周期
    **静默降级为日线**（`m.get(p, "day")`）。结果是用户点「月线/季线」看到的
    是日线数据，且错误数据以 `period` 为键写入 K 线缓存造成污染。

契约原则：
    1. 单点定义：所有周期能力只在本文件声明，别处一律引用。
    2. 显式失败：未知周期抛 ValueError，**绝不静默降级**（错误数据比报错危险得多）。
    3. 契约驱动 UI：`all_periods()` 供 `/market/periods` 端点返回，前端据此渲染
       周期条并对不支持的周期置灰。

数据源实测结论（2026-08-29，标的 600519.SH，见 tests/probe_periods.py）：
    - 月线：传 `1mo` 会被服务端当无效值处理、退回日线（间隔 1 天）；
      传 `month` / `1mon` / `mo` 返回真月线（间隔 31 天）→ canonical `1mo` 映射为 `month`。
    - 季线：`1q` / `quarter` / `q` / `3mo` / `season` **全部返回日线**（间隔 1 天），
      eltdx 不支持季线 → 标记 supported=False，前端置灰。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# 分时不走 K 线端点，单独声明（前端周期条需与 K 线周期同列渲染）
TICK_PERIOD = "tick"


@dataclass(frozen=True)
class PeriodSpec:
    """一个周期的契约描述。"""

    canonical: str          # 契约值：前后端传输唯一使用此值
    label: str              # 中文短标签（前端周期条按钮文案）
    kind: str               # "minute" | "kline"
    eltdx: Optional[str]    # eltdx bars.get 的 period 入参；None = 该源不支持
    supported: bool = True  # 是否有可用数据源
    reason: str = ""        # 不支持时的原因（前端 tooltip 展示）
    # 相邻 bar 的期望间隔（天），用于契约回归校验；分钟线为 None
    expect_gap_days: Optional[float] = None


# ============================ 契约表（唯一真相） ============================
CANONICAL_PERIODS: Dict[str, PeriodSpec] = {
    "1m": PeriodSpec("1m", "1分", "minute", "1m", expect_gap_days=None),
    "5m": PeriodSpec("5m", "5分", "minute", "5m", expect_gap_days=None),
    "15m": PeriodSpec("15m", "15分", "minute", "15m", expect_gap_days=None),
    "30m": PeriodSpec("30m", "30分", "minute", "30m", expect_gap_days=None),
    "60m": PeriodSpec("60m", "60分", "minute", "60m", expect_gap_days=None),
    "1d": PeriodSpec("1d", "日线", "kline", "day", expect_gap_days=1.0),
    "1w": PeriodSpec("1w", "周线", "kline", "week", expect_gap_days=7.0),
    "1mo": PeriodSpec("1mo", "月线", "kline", "month", expect_gap_days=30.0),
    "1q": PeriodSpec(
        "1q", "季线", "kline", None, supported=False,
        reason="eltdx 数据源实测不支持季线（请求均返回日线），已禁用以免静默降级为日线",
        expect_gap_days=None,
    ),
    "1y": PeriodSpec("1y", "年线", "kline", "year", expect_gap_days=365.0),
}

# 历史别名 → canonical（入向兼容：老前端/老脚本发的非标准值先归一化，不报错）
ALIASES: Dict[str, str] = {
    # 分钟
    "1min": "1m", "min": "1m", "minute": "1m", "m1": "1m",
    "5min": "5m", "m5": "5m",
    "15min": "15m", "m15": "15m",
    "30min": "30m", "m30": "30m",
    "60min": "60m", "m60": "60m", "1h": "60m", "hour": "60m",
    # 日
    "day": "1d", "d": "1d", "daily": "1d",
    # 周
    "week": "1w", "w": "1w", "weekly": "1w",
    # 月
    "1mon": "1mo", "mon": "1mo", "month": "1mo", "mo": "1mo", "monthly": "1mo",
    # 季
    "quarter": "1q", "q": "1q", "3mo": "1q", "3mon": "1q", "season": "1q", "quarterly": "1q",
    # 年
    "year": "1y", "y": "1y", "1year": "1y", "annual": "1y", "annually": "1y",
}


class UnknownPeriodError(ValueError):
    """未知周期。必须显式报错，禁止静默降级为日线。"""


class UnsupportedPeriodError(ValueError):
    """周期契约存在，但当前数据源不支持（如季线）。"""


def _hint() -> str:
    return "可用周期: " + ", ".join([TICK_PERIOD] + list(CANONICAL_PERIODS))


def normalize_period(period: Optional[str]) -> str:
    """把任意写法（含历史别名）归一化为 canonical 值。

    未知值抛 UnknownPeriodError —— 绝不 fallback 到 "1d"。
    """
    p = (period or "").strip().lower()
    if not p:
        raise UnknownPeriodError(f"周期不能为空（{_hint()}）")
    if p in CANONICAL_PERIODS:
        return p
    if p in ALIASES:
        return ALIASES[p]
    raise UnknownPeriodError(f"不支持的周期: {period}（{_hint()}）")


def spec(period: str) -> PeriodSpec:
    """取周期契约描述（会先归一化）。"""
    return CANONICAL_PERIODS[normalize_period(period)]


def to_eltdx_period(period: str) -> str:
    """canonical → eltdx 入参。不支持/未知均抛错，不静默降级。"""
    s = spec(period)
    if not s.supported or not s.eltdx:
        raise UnsupportedPeriodError(
            f"周期 {s.canonical}（{s.label}）当前数据源不支持"
            + (f"：{s.reason}" if s.reason else "")
        )
    return s.eltdx


def is_supported(period: str) -> bool:
    """该周期是否有可用数据源（未知周期返回 False，不抛错，供 UI 判断）。"""
    try:
        return spec(period).supported
    except UnknownPeriodError:
        return False


def all_periods() -> List[dict]:
    """供 `GET /market/periods` 返回，前端周期条据此渲染（契约驱动 UI）。"""
    items = [{
        "v": TICK_PERIOD,
        "label": "分时",
        "kind": "tick",
        "supported": True,
        "reason": "",
    }]
    for s in CANONICAL_PERIODS.values():
        items.append({
            "v": s.canonical,
            "label": s.label,
            "kind": s.kind,
            "supported": s.supported,
            "reason": s.reason,
        })
    return items


def kline_periods() -> List[str]:
    """参与 K 线缓存/同步任务的周期（不含分时）。"""
    return [s.canonical for s in CANONICAL_PERIODS.values() if s.supported]


def adjust_allowed_periods() -> tuple:
    """支持复权（qfq/hfq）的周期 —— registry 引用此常量，避免第三份硬编码。

    复权数据来自补充源（eltdx），日/周/月/年口径可用；分钟线复权口径不一致，不支持。
    """
    return tuple(
        s.canonical for s in CANONICAL_PERIODS.values()
        if s.supported and s.kind == "kline"
    )
