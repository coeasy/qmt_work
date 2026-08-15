"""交易日历感知的调度工具（引擎降频优化）。

背景：条件单/涨停监控/对账/健康检查等引擎在非交易时段高频轮询券商是无效开销，
且会增加券商端连接压力。本模块统一提供「交易日 + 交易时段」判断，
让各引擎在盘中按业务间隔轮询、非交易时段降频探活。

交易日判定优先级：
1. refresh_from_calendar() 注入的真实交易日历（券商 get_trading_calendar 拉取）；
2. 未注入时回退「周一至周五」周末规则（不排除法定节假日，可接受：降频不影响正确性）。

交易时段（A 股，边界放宽 5 分钟）：
- 盘中活跃：9:15–11:35、13:00–15:05（覆盖集合竞价与收盘）
- 其余（午休/盘前盘后/夜间）为休眠期
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

log = logging.getLogger("qmt_work")

_AM_START = time(9, 15)
_AM_END = time(11, 35)
_PM_START = time(13, 0)
_PM_END = time(15, 5)


class TradingSession:
    """全局单例式交易时段判断器（可由真实交易日历刷新）。"""

    def __init__(self, calendar: list[str] | None = None):
        # YYYYMMDD 字符串集合
        self._calendar: set[str] | None = None
        if calendar:
            self.refresh_from_calendar(calendar)

    def refresh_from_calendar(self, calendar: list[str]) -> int:
        """注入券商真实交易日历（YYYYMMDD 列表），返回注入条数。"""
        days = {str(x) for x in calendar if x}
        if days:
            self._calendar = days
            log.info("trading calendar loaded: %d days", len(days))
        return len(days)

    def use_fallback(self) -> None:
        """显式回退周末规则（券商日历不可用时调用）。"""
        self._calendar = None

    # ---------------- 判定 ----------------
    def is_trading_day(self, d: date | None = None) -> bool:
        d = d or date.today()
        if self._calendar:
            return d.strftime("%Y%m%d") in self._calendar
        return d.weekday() < 5          # 周一~周五

    def in_active_hours(self, now: datetime | None = None) -> bool:
        """是否处于盘中活跃时段（9:15–11:35 / 13:00–15:05）。"""
        now = now or datetime.now().astimezone()
        t = now.time()
        return (_AM_START <= t <= _AM_END) or (_PM_START <= t <= _PM_END)

    def is_active(self, now: datetime | None = None) -> bool:
        """是否应保持高频轮询：交易日 && 盘中活跃。"""
        now = now or datetime.now().astimezone()
        return self.is_trading_day(now.date()) and self.in_active_hours(now)

    def sleep_seconds(self, active: float, idle: float,
                      now: datetime | None = None) -> float:
        """返回本轮应休眠秒数：盘中用 active，休眠期用 idle（探活）。"""
        return active if self.is_active(now) else max(idle, 1.0)

    # ---------------- 统计 ----------------
    def stats(self) -> dict:
        mode = "calendar" if self._calendar else "weekday-fallback"
        return {"mode": mode,
                "days": len(self._calendar) if self._calendar else 0,
                "active_now": self.is_active()}


# 进程级默认实例（引擎循环可直接使用）
default_session = TradingSession()
