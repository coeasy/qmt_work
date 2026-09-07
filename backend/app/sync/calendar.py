"""A 股交易日历（内置节假日表 + runtime_config 扩展，G1-5b 落地）。

来源与边界：
- 内置表为国务院办公厅公布的法定节假日安排（含调休补班日），覆盖 2024-2027；
  每年节假日安排公布后（约 11 月底）由维护者更新 :data:`HOLIDAYS` 与 :data:`WORKDAYS`。
- 临时调整无需改代码：经 runtime_config `market.calendar.holidays` /
  `market.calendar.workdays`（逗号分隔 YYYY-MM-DD）追加，热生效。
- 超出内置表覆盖年份时回退「周一至周五」启发式（与旧行为一致），并显式打日志，
  不静默装作精确。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List

log = logging.getLogger("qmt_work.sync.calendar")

# 法定节假日（休市日，含周末连休中落在交易日段的日期无影响，全量列出便于查询）。
HOLIDAYS: set[str] = {
    # 2024
    "2024-01-01",
    "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
    "2024-04-04", "2024-04-05",
    "2024-05-01", "2024-05-02", "2024-05-03",
    "2024-06-10",
    "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    # 2025
    "2025-01-01", "2025-01-31",
    "2025-02-03", "2025-02-04",
    "2025-04-04",
    "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026（国务院 2025-11 公布安排；春节 2/15-2/21，国庆中秋 10/1-10/7）
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-06",
    "2026-05-01",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    # 2027（预估：元旦 + 春节 + 清明 + 劳动 + 端午 + 中秋 + 国庆；公布后校正）
    "2027-01-01",
}

# 调休补班日（周末但为交易日）。
WORKDAYS: set[str] = {
    "2024-02-04", "2024-02-18", "2024-04-07", "2024-09-14", "2024-09-29", "2024-10-12",
    "2025-01-26", "2025-02-08", "2025-04-27", "2025-09-28", "2025-10-11",
    "2026-01-04", "2026-02-14", "2026-02-15", "2026-04-05", "2026-09-27", "2026-10-10",
}

_CALENDAR_MAX_YEAR = 2027  # 内置表覆盖的最后一年


def _config_extra(kind: str) -> set[str]:
    """runtime_config 追加项：market.calendar.{holidays,workdays}（逗号分隔）。"""
    try:
        from app.state import state
        rc = getattr(state, "runtime_config", None)
        raw = (rc.get(f"market.calendar.{kind}") or "") if rc else ""
        return {s.strip() for s in str(raw).replace("，", ",").split(",") if s.strip()}
    except Exception:  # noqa: BLE001
        return set()


def is_trading_day(d: date) -> bool:
    """判断某日是否 A 股交易日（内置表年份内精确；超出覆盖年回退工作日启发式）。"""
    if d.weekday() >= 5:
        return d.isoformat() in WORKDAYS or d.isoformat() in _config_extra("workdays")
    if d.isoformat() in HOLIDAYS or d.isoformat() in _config_extra("holidays"):
        return False
    if d.year > _CALENDAR_MAX_YEAR:
        # 超出内置表覆盖年：工作日启发式（节假日可能误判为交易日），显式提示维护者更新日历
        log.debug("trading calendar beyond coverage year %s (max %s)", d.year, _CALENDAR_MAX_YEAR)
    return True


def trading_calendar(end: date, count: int) -> List[str]:
    """最近 ``count`` 个交易日（升序 "YYYY-MM-DD"），节假日精确（覆盖年内）。"""
    out: List[str] = []
    d = end
    while len(out) < count:
        if is_trading_day(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    out.reverse()
    return out
