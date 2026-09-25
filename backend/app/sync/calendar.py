"""A 股交易日历（内置节假日表 + runtime_config 扩展，G1-5b 落地）。

来源与边界：
- 内置表为沪深北交易所公布的法定节假日休市安排，覆盖 2024-2026；
  每年安排公布后（约 12 月）由维护者更新 :data:`HOLIDAYS`（与 :data:`WORKDAYS`）。
- 临时调整无需改代码：经 runtime_config `market.calendar.holidays` /
  `market.calendar.workdays`（逗号分隔 YYYY-MM-DD）追加，热生效。
- 超出内置表覆盖年份时回退「周一至周五」启发式（与旧行为一致），并显式打日志，
  不静默装作精确。

★★ R26 修复：**A 股周末一律休市**。交易所休市通知里那些「另外，X 月 X 日
（星期X）为周末休市」的日期是**调休补班日**（全社会上班），但证券市场的
交易/清算是照休的 —— 例如 2026 年通知原文：「1 月 4 日（星期日）为周末休市」。
旧实现把这些周末补班日塞进 :data:`WORKDAYS` 并判定为交易日，后果是：
  · `is_trading_day` 在周末返回 True ⇒ `/market/session` 声称「今天是交易日」；
  · `TradingSession.is_active()` 周末 9:15-15:05 为 True ⇒ 引擎整天高频空转打源；
  · `prev/next_trading_day` 会把周末补班日当作参照日。
故内置 :data:`WORKDAYS` 清空；仅保留 runtime_config `market.calendar.workdays`
作为**非常规**扩展口（如未来出现真正的周末交易安排）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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
    # 2025（春节 1/28-2/4；国庆中秋 10/1-10/8）
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04",
    "2025-04-04",
    "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026（沪深北交易所 2025-12-22 通知：深证会〔2025〕481 号 / 上证公告〔2025〕45 号）
    #  仅列**工作日**休市段；周末日（1/4、2/14、2/28、5/9、9/20、10/10）
    #  已由 `d.weekday() >= 5` 覆盖，不必重复列出。
    "2026-01-01", "2026-01-02",                                  # 元旦 1/1-1/3（1/3 为周六）
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",      # 春节 2/15-2/23
    "2026-02-20", "2026-02-23",                                  #  ★ 2/23 周一原缺
    "2026-04-06",                                                # 清明 4/4-4/6（4/6 周一）
    "2026-05-01", "2026-05-04", "2026-05-05",                    # ★ 劳动 5/1-5/5（5/4、5/5 原缺）
    "2026-06-19",                                                # 端午 6/19-6/21（6/19 周五）
    "2026-09-25",                                                # 中秋 9/25-9/27（9/25 周五）
    "2026-10-01", "2026-10-02", "2026-10-05",                    # 国庆 10/1-10/7
    "2026-10-06", "2026-10-07",
}

# 调休补班日（周末）—— **A 股不交易**，故内置为空。
#
# 交易所休市通知里的「周末休市」日（如 2026-01-04 周日、2026-10-10 周六）
# 属调休补班日：全社会上班，但证券交易/清算照休。历史上这里列过这些日期并
# 判定为交易日，是**错误**的（详见模块 docstring 的 R26 说明）。
# 该常量与 runtime_config `market.calendar.workdays` 的组合机制保留，
# 供未来真出现「周末交易」安排时使用。
WORKDAYS: set[str] = set()

_CALENDAR_MAX_YEAR = 2026  # 内置表覆盖的最后一年
# ★ R26：原为 2027，但 2027 年安排要到 2026 年底才公布（当时仅填了元旦 1 天），
#   `coverage().exact=True` 属虚假精度 —— 上层据此判定「精确」会得到错误结论。
#   2027 起由「周一~周五」启发式兜底并打日志，诚实标注不精确。


@dataclass(frozen=True)
class CalendarCoverage:
    """日历结果的覆盖声明，禁止调用方把启发式日期当成精确日历。"""

    start_year: int
    end_year: int
    exact: bool
    source: str


class CalendarCoverageError(ValueError):
    """请求范围超出精确交易所日历覆盖，禁止静默用工作日近似。"""


class ExchangeCalendarPort:
    """交易所日历端口（Phase 2 SSOT）。

    当前实现由内置法定节假日表 + runtime_config 组成；券商/交易所日历接入后可
    替换此端口而不改变同步器调用方式。超出内置覆盖范围仍可计算工作日，但通过
    ``coverage`` 明确标记 ``exact=False``，上层不得把它宣称为交易所精确结果。
    """

    def coverage(self, d: date) -> CalendarCoverage:
        exact = 2024 <= d.year <= _CALENDAR_MAX_YEAR
        return CalendarCoverage(
            start_year=2024, end_year=_CALENDAR_MAX_YEAR,
            exact=exact, source="builtin+runtime_config" if exact else "weekday-fallback",
        )

    def is_trading_day(self, d: date) -> bool:
        return is_trading_day(d)

    def trading_calendar(self, end: date, count: int) -> List[str]:
        return trading_calendar(end, count)

    def require_exact(self, start: date, end: date) -> None:
        if not self.coverage(start).exact or not self.coverage(end).exact:
            raise CalendarCoverageError(
                f"交易日历精确覆盖范围为 2024-{_CALENDAR_MAX_YEAR}，"
                f"请求 {start.isoformat()}..{end.isoformat()} 需要真实交易所日历源"
            )

    def persist(self, db, *, market: str = "CN", exchange: str = "SSE/SZSE",
                start: date, end: date, version: str = "builtin-2024-2026") -> int:
        """把当前 SSOT 结果落库，供 scheduler/backtest/research 共用。"""
        self.require_exact(start, end)
        rows = [(market, exchange, dt, "regular", "builtin+runtime_config", version)
                for dt in trading_calendar(end, (end - start).days + 1)
                if dt >= start.isoformat()]
        db.executemany(
            "INSERT OR REPLACE INTO exchange_calendar "
            "(market,exchange,trade_date,session,calendar_source,calendar_version) "
            "VALUES (?,?,?,?,?,?)", rows)
        return len(rows)


def _config_extra(kind: str) -> set[str]:
    """runtime_config 追加项：market.calendar.{holidays,workdays}（逗号分隔）。"""
    try:
        from core.state import state
        rc = getattr(state, "runtime_config", None)
        raw = (rc.get(f"market.calendar.{kind}") or "") if rc else ""
        return {s.strip() for s in str(raw).replace("，", ",").split(",") if s.strip()}
    except Exception:  # noqa: BLE001
        return set()


def is_trading_day(d: date) -> bool:
    """判断某日是否 A 股交易日（内置表年份内精确；超出覆盖年回退工作日启发式）。

    ★ R26：周末分支只认 runtime_config `market.calendar.workdays`（内置 WORKDAYS
    已清空）—— A 股周末一律休市，调休补班日不交易。
    """
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


# ---------------- 权威判定与「参照交易日」 ----------------
#
# 背景：`is_trading_day` 只看内置节假日表 + 工作日启发式。券商日历一旦注入
# （`gateway.trading_session.refresh_from_calendar`，实测 8728 天），临时休市 /
# 调休这类只有交易所知道的变化以内置表是判不出来的。所以对外提供
# `is_trading_day_exact` 作为**唯一权威入口**，优先级：券商日历 > 内置表。


def has_exchange_calendar() -> bool:
    """券商是否已注入真实交易日历。"""
    try:
        from gateway.trading_session import default_session
        return default_session.has_calendar
    except Exception:  # noqa: BLE001
        return False


def is_trading_day_exact(d: date) -> bool:
    """交易日判定的权威入口：券商真实日历 > 内置节假日表。

    新增代码请一律用它，不要直接调 :func:`is_trading_day` —— 后者在券商日历
    已注入且与内置表不一致时会给出错误答案。

    ★ 覆盖区间是必须的：券商日历默认只下发到「最后一个已公布交易日」
    （实测 QMT `get_trading_calendar()` 无参调用返回 19901219..20260918）。
    区间**外**的日期不在集合里，若照「在不在集合里」判定，2026-09-21（周一）
    这种未来交易日会被判成休市 —— `next_trading_day` 会走满 30 天扫描上限后
    返回 20261020 这种荒谬结果。故区间外一律退回内置节假日表。
    """
    if has_exchange_calendar():
        try:
            from gateway.trading_session import default_session
            if default_session.covers(d):
                return default_session.is_trading_day(d)
        except Exception:  # noqa: BLE001
            pass
    return is_trading_day(d)


def prev_trading_day(d: date, *, include_self: bool = True) -> date:
    """不晚于 ``d`` 的最近交易日（``include_self=False`` 时严格早于 ``d``）。

    扫描上限 30 天：A 股最长连续休市（春节 + 周末）也在其内，超限即返回
    当前游标而不是死循环。
    """
    cur = d if include_self else d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day_exact(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def next_trading_day(d: date, *, include_self: bool = False) -> date:
    """严格晚于 ``d`` 的第一个交易日（``include_self=True`` 时含 ``d``）。"""
    cur = d if include_self else d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day_exact(cur):
            return cur
        cur += timedelta(days=1)
    return cur


def session_snapshot(now=None) -> dict:
    """当前交易会话的权威快照（``GET /market/session`` 的唯一数据来源）。

    为什么需要它：非交易日打开行情页时，后端各接口照常返回**上一交易日**的
    数据（这是正确的），但界面没有一处告诉用户「这是 20260918 的数据」，
    用户看到的是「今天（周六）的行情」——数字对、语义错。本快照把
    「今天是什么日子 / 该看哪一天的数据 / 下一个交易日是哪天」一次性说清楚。

    ``last_trading_day`` 同时充当数据参照日（``as_of``）：交易日当天就是今天
    （盘中数据在更新，收盘后即最终值），非交易日回退到上一交易日。
    """
    from core.clock import local_now

    now = now or local_now()
    today = now.date()
    trading_day = is_trading_day_exact(today)
    ref = today if trading_day else prev_trading_day(today, include_self=False)

    phase = "holiday"
    mode = "builtin"
    try:
        from gateway.trading_session import default_session
        mode = "exchange" if default_session.has_calendar else "builtin"
        phase = default_session.phase(now)
        # ★ R26：`trading_day` 是本函数里的**权威**判定（is_trading_day_exact：券商日历 >
        #   内置表），而 `phase()` 内部走的是 TradingSession 自己的判定。两者共用同一
        #   日历却各调一次，一旦口径分叉就会产出**自相矛盾**的信封 —— 实测 2026-09-25
        #   （中秋，工作日休市）返回 `trading_day=False` 却 `phase="closed"`，界面据此
        #   显示「已收盘」而不是「休市」。这里以 trading_day 为准强制归一到 holiday，
        #   让契约（非交易日 ⇒ phase=="holiday"）由构造保证，而不是靠两个来源恰好一致。
        if not trading_day:
            phase = "holiday"
    except Exception:  # noqa: BLE001
        phase = "open" if trading_day else "holiday"

    return {
        # 今天（YYYYMMDD）
        "today": today.strftime("%Y%m%d"),
        # 今天是否交易日（节假日/周末为 false）
        "trading_day": trading_day,
        # 是否盘中活跃（交易日 && 9:15–11:35 / 13:00–15:05）
        "active": bool(trading_day and phase == "open"),
        # 会话阶段：holiday / pre_open / open / lunch_break / closed
        "phase": phase,
        # 数据参照日 = 该看哪一天的行情（非交易日回退上一交易日）
        "last_trading_day": ref.strftime("%Y%m%d"),
        "as_of": ref.strftime("%Y%m%d"),
        # 下一个交易日（今天已是交易日时为**今天之后**的那一个）
        "next_trading_day": next_trading_day(today).strftime("%Y%m%d"),
        # 日历来源，供界面标注可信度
        "calendar": {"mode": mode, "exact": today.year <= _CALENDAR_MAX_YEAR},
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


# 进程内唯一日历端口；函数保留以兼容现有调用方。
exchange_calendar = ExchangeCalendarPort()


__all__ = [
    "CalendarCoverage", "CalendarCoverageError", "ExchangeCalendarPort", "exchange_calendar",
    "HOLIDAYS", "WORKDAYS", "is_trading_day", "trading_calendar",
    # 权威判定与参照交易日（V11 R14）
    "has_exchange_calendar", "is_trading_day_exact",
    "prev_trading_day", "next_trading_day", "session_snapshot",
]
