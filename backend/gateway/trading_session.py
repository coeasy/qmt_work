"""交易日历感知的调度工具（引擎降频优化）。

背景：条件单/涨停监控/对账/健康检查等引擎在非交易时段高频轮询券商是无效开销，
且会增加券商端连接压力。本模块统一提供「交易日 + 交易时段」判断，
让各引擎在盘中按业务间隔轮询、非交易时段降频探活。

交易日判定优先级：
1. refresh_from_calendar() 注入的真实交易日历（券商 get_trading_calendar 拉取），
   且日期落在该日历的覆盖区间内；
2. 区间外或未注入时取内置节假日表（app.sync.calendar，覆盖 2024-2026 且含周末规则）；
3. 内置表不可用时才退回「周一至周五」周末规则（不排除法定节假日，可接受：
   降频不影响正确性）。

交易时段（A 股，边界放宽 5 分钟）：
- 盘中活跃：9:15–11:35、13:00–15:05（覆盖集合竞价与收盘）
- 其余（午休/盘前盘后/夜间）为休眠期
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from core.clock import local_now

log = logging.getLogger("qmt_work")

_AM_START = time(9, 15)
_AM_END = time(11, 35)
_PM_START = time(13, 0)
_PM_END = time(15, 5)


def _builtin_is_trading_day():
    """内置节假日表判定函数（延迟导入，避免 gateway → app 的模块级循环依赖）。

    拿不到（导入失败）时返回 None，调用方退回周末规则。
    """
    try:
        from app.sync.calendar import is_trading_day as _f
        return _f
    except Exception:  # noqa: BLE001
        return None


class TradingSession:
    """全局单例式交易时段判断器（可由真实交易日历刷新）。"""

    def __init__(self, calendar: list[str] | None = None):
        # YYYYMMDD 字符串集合
        self._calendar: set[str] | None = None
        # 已注入日历的覆盖区间 (最早, 最晚)；未注入为 None
        self._span: tuple[str, str] | None = None
        if calendar:
            self.refresh_from_calendar(calendar)

    def refresh_from_calendar(self, calendar: list[str]) -> int:
        """注入券商真实交易日历（YYYYMMDD 列表），返回注入条数。"""
        days = {str(x) for x in calendar if x}
        if days:
            self._calendar = days
            # ★ 覆盖区间必须一起记下来。券商日历**只下发到最后一个已公布交易日**
            #   （实测 QMT 默认区间 = 19901219..20260918），区间之外的日期根本不在
            #   集合里 —— 「不在集合里」≠「不是交易日」。详见 is_trading_day。
            self._span = (min(days), max(days))
            log.info("trading calendar loaded: %d days (%s..%s)", len(days), *self._span)
        return len(days)

    def use_fallback(self) -> None:
        """显式回退周末规则（券商日历不可用时调用）。"""
        self._calendar = None
        self._span = None

    @property
    def has_calendar(self) -> bool:
        """是否已注入券商真实交易日历。

        调用方据此决定「谁更可信」：已注入时本实例的判定优先于内置节假日表
        （临时休市/调休只有交易所日历知道）；未注入时内置表更准。
        """
        return bool(self._calendar)

    @property
    def calendar_span(self) -> tuple[str, str] | None:
        """已注入日历的覆盖区间 ``(最早, 最晚)``（YYYYMMDD）；未注入返回 None。"""
        return self._span

    def covers(self, d: date) -> bool:
        """该日期是否落在已注入日历的覆盖区间内。

        区间**外**不能用「在不在集合里」判定交易日 —— 那只会得到「不是」，
        而正确答案很可能是「是，只是券商没下发那么远」。
        """
        if not self._span:
            return False
        key = d.strftime("%Y%m%d")
        return self._span[0] <= key <= self._span[1]

    # ---------------- 判定 ----------------
    def is_trading_day(self, d: date | None = None) -> bool:
        # V11 R8：``date.today()`` 是第二份「当前日期」实现，统一取 core.clock。
        d = d or local_now().date()
        if self._calendar and self.covers(d):
            key = d.strftime("%Y%m%d")
            return key in self._calendar
        # ★ 覆盖区间外（最常见的是**未来日期**：券商日历默认只到「最后一个已公布
        #   交易日」）不能直接返回 False —— 后果是**所有未来交易日都被判成休市**：
        #     · 「下一个交易日」永远找不到（实测 next_trading_day 走满 30 天
        #       扫描上限后返回 20261020 这种荒谬结果）；
        #     · 客户端跨周末连续运行到周一时，is_active() 整天为 False，
        #       引擎一整天不轮询，行情停更且没有任何报错。
        #   ★★ R26 修复：**无券商日历**时同样不能退回「周一~周五」—— 那会把
        #   工作日节假日（中秋 / 国庆 / 春节调休段）判成交易日，直接后果有两个：
        #     · `phase()` 在节假日返回 "closed" 而不是 "holiday"（界面提示错）；
        #     · `is_active()` 在节假日 9:15-15:05 为 True ⇒ 引擎整天高频空转打源。
        #   故一律先取内置节假日表（覆盖 2024-2026，比周末规则精确），
        #   真拿不到内置表时才退回周末规则。
        builtin = _builtin_is_trading_day()
        if builtin is not None:
            return builtin(d)
        return d.weekday() < 5          # 周一~周五（最后的兜底）

    def in_active_hours(self, now: datetime | None = None) -> bool:
        """是否处于盘中活跃时段（9:15–11:35 / 13:00–15:05）。"""
        now = now or local_now()
        t = now.time()
        return (_AM_START <= t <= _AM_END) or (_PM_START <= t <= _PM_END)

    def is_active(self, now: datetime | None = None) -> bool:
        """是否应保持高频轮询：交易日 && 盘中活跃。"""
        now = now or local_now()
        return self.is_trading_day(now.date()) and self.in_active_hours(now)

    def phase(self, now: datetime | None = None) -> str:
        """当前会话阶段：holiday / pre_open / open / lunch_break / closed。

        比 ``is_active()`` 的布尔值多一层信息：``active=False`` 同时覆盖
        「今日休市」「盘前」「午休」「已收盘」四种完全不同的用户预期，
        界面只说「非交易时段」会让用户以为数据不该有。分阶段才能给出
        正确引导（如收盘后提示「已收盘 · 显示 20260918 收盘数据」）。
        """
        now = now or local_now()
        if not self.is_trading_day(now.date()):
            return "holiday"
        t = now.time()
        if t < _AM_START:
            return "pre_open"
        if t <= _AM_END:
            return "open"
        if t < _PM_START:
            return "lunch_break"
        if t <= _PM_END:
            return "open"
        return "closed"

    def sleep_seconds(self, active: float, idle: float,
                      now: datetime | None = None) -> float:
        """返回本轮应休眠秒数：盘中用 active，休眠期用 idle（探活）。"""
        return active if self.is_active(now) else max(idle, 1.0)

    # ---------------- 统计 ----------------
    def stats(self) -> dict:
        mode = "calendar" if self._calendar else "weekday-fallback"
        return {"mode": mode,
                "days": len(self._calendar) if self._calendar else 0,
                # 覆盖区间一并暴露：只报条数看不出「日历到哪天为止」，
                # 而区间外判定会退回内置表 —— 不透明的话排查时完全摸不着头脑。
                "span": list(self._span) if self._span else None,
                "active_now": self.is_active()}


# 进程级默认实例（引擎循环可直接使用）
default_session = TradingSession()
