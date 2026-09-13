"""V9 Phase 7（D-F）：自研 5 段 cron 解析器（不引入 apscheduler）。

支持：``*``、``*/n``、``a-b``、``a,b,c`` 及组合；周字段 0=周日（含 7）。
语义与 vixie-cron 一致：「日」与「周」同时受限时为 **OR**（任一匹配即触发），
这是 cron 的经典语义（本实现遵循 vixie 行为并在测试中钉死）。

    CronExpr.parse("30 18 * * 1-5")   # 工作日 18:30
    expr.next_after(dt)               # -> datetime | None（严格晚于 dt 的下一次触发）
    expr.matches(dt)                  # -> bool
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELDS = ("minute", "hour", "day", "month", "dow")
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4,
              "fri": 5, "sat": 6}


def _parse_field(field: str, lo: int, hi: int, is_dow: bool = False) -> frozenset[int]:
    """解析单字段为合法值集合。"""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip().lower()
        if not part:
            raise ValueError(f"empty cron field part in {field!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"cron step must be positive: {field!r}")
        if part == "*" or part == "":
            start, end = lo, hi
        elif "-" in part and not part.lstrip("-").isdigit():
            a, b = part.split("-", 1)
            start, end = _val(a, lo, hi, is_dow), _val(b, lo, hi, is_dow)
            if start > end:
                raise ValueError(f"inverted cron range: {part!r}")
        else:
            start = end = _val(part, lo, hi, is_dow)
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"cron field has no values: {field!r}")
    return frozenset(values)


def _val(tok: str, lo: int, hi: int, is_dow: bool) -> int:
    if is_dow and tok in _DOW_NAMES:
        v = _DOW_NAMES[tok]
    else:
        v = int(tok)
    if is_dow and v == 7:      # 7 == 周日（vixie 兼容）
        v = 0
    if not lo <= v <= hi:
        raise ValueError(f"cron value {v} out of range [{lo},{hi}]")
    return v


@dataclass(frozen=True)
class CronExpr:
    """解析后的 cron 表达式。"""

    raw: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]       # 空 = 日字段为 *（仅周限制）
    months: frozenset[int]
    dows: frozenset[int]       # 空 = 周字段为 *（仅日限制）
    day_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, expr: str) -> "CronExpr":
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expr must have 5 fields: {expr!r}")
        minutes = _parse_field(parts[0], 0, 59)
        hours = _parse_field(parts[1], 0, 23)
        day_star = parts[2] == "*"
        days = frozenset() if day_star else _parse_field(parts[2], 1, 31)
        months = _parse_field(parts[3], 1, 12)
        dow_star = parts[4] == "*"
        dows = frozenset() if dow_star else _parse_field(parts[4], 0, 6, is_dow=True)
        return cls(expr, minutes, hours, days, months, dows, day_star, dow_star)

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes or dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False
        day_ok = self.day_star or dt.day in self.days
        dow_ok = self.dow_star or (dt.weekday() + 1) % 7 in self.dows
        if self.day_star and self.dow_star:
            return True
        if self.day_star:
            return dow_ok
        if self.dow_star:
            return day_ok
        return day_ok or dow_ok     # vixie 语义：日/周同时受限 = OR

    def next_after(self, dt: datetime, horizon_days: int = 400) -> datetime | None:
        """严格晚于 dt 的下一次触发时刻（秒位清零）。超过 horizon 返回 None。"""
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = dt + timedelta(days=horizon_days)
        while t <= limit:
            if self.matches(t):
                return t
            # 优化：分钟/小时都不在集合内时直接跳到下一个整点
            if t.hour not in self.hours and t.minute != 0:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.month not in self.months:
                # 跳到下月 1 日 00:00
                y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                t = t.replace(year=y, month=m, day=1, hour=0, minute=0)
                continue
            t += timedelta(minutes=1)
        return None


def validate(expr: str) -> None:
    CronExpr.parse(expr)


__all__ = ["CronExpr", "validate"]
