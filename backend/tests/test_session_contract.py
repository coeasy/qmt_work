"""`GET /api/v1/market/session` 契约 + 交易日历「参照日」语义。

为什么需要这个端点：非交易日（周末/节假日）后端各行情接口照常返回**上一交易日**
的数据 —— 这是正确的行为，但界面此前没有任何地方标注这一点，用户会把周六看到
的数字当成「今天的行情」。`/health.trading_session` 回答不了这个问题（只有
mode/active/trading_day 三个字段，没有日期），所以单开一个端点给出
`last_trading_day` / `as_of` / `next_trading_day`。

本文件锁三件事：
1. 契约形状（字段名、类型、信封）；
2. **参照日语义**（交易日 = 今天；非交易日 = 上一交易日；绝不指向未来）；
3. 权威判定优先级（券商日历 > 内置节假日表）。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.sync.calendar import (
    is_trading_day,
    is_trading_day_exact,
    next_trading_day,
    prev_trading_day,
    session_snapshot,
)

PHASES = {"holiday", "pre_open", "open", "lunch_break", "closed"}


def _data(app_client) -> dict:
    r = app_client.get("/api/v1/market/session")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("code") == 0, body
    return body.get("data") or {}


# ---------------- 契约形状 ----------------


def test_session_endpoint_shape(app_client):
    """字段名必须与前端 `SessionSnapshot` 逐一对齐（snake_case）。

    名字对不上时前端既不报错也不兜底，只会静默少一个日期 —— 本项目反复踩的
    静默失配，所以契约必须钉死。
    """
    d = _data(app_client)
    assert set(d) >= {
        "today", "trading_day", "active", "phase",
        "last_trading_day", "as_of", "next_trading_day",
        "calendar", "now",
    }, d
    assert isinstance(d["trading_day"], bool), d
    assert isinstance(d["active"], bool), d
    assert d["phase"] in PHASES, d
    assert isinstance(d["calendar"], dict)
    assert set(d["calendar"]) >= {"mode", "exact"}, d["calendar"]


def test_session_dates_are_bar_date_format(app_client):
    """日期一律 YYYYMMDD（落库/比较的唯一格式，见 core.clock.bar_date）。"""
    d = _data(app_client)
    for key in ("today", "last_trading_day", "as_of", "next_trading_day"):
        v = d[key]
        assert isinstance(v, str) and len(v) == 8 and v.isdigit(), (key, v)


def test_session_active_implies_trading_day(app_client):
    """不变量：盘中 ⇒ 今日必然是交易日。"""
    d = _data(app_client)
    if d["active"]:
        assert d["trading_day"] is True, d


def test_session_phase_consistent_with_flags(app_client):
    """phase 与 (trading_day, active) 必须自洽，不允许互相矛盾。"""
    d = _data(app_client)
    if not d["trading_day"]:
        assert d["phase"] == "holiday", d
    else:
        assert d["phase"] != "holiday", d
        assert d["active"] is (d["phase"] == "open"), d


# ---------------- 参照日语义（核心） ----------------


def test_as_of_is_last_trading_day(app_client):
    """`as_of` 与 `last_trading_day` 同源：界面「数据截至」取哪个都不会错。"""
    d = _data(app_client)
    assert d["as_of"] == d["last_trading_day"], d


def test_trading_day_uses_today_as_reference(app_client):
    """今日是交易日 ⇒ 参照日就是今天（不回退上一交易日）。"""
    d = _data(app_client)
    if d["trading_day"]:
        assert d["last_trading_day"] == d["today"], d


def test_off_day_falls_back_to_previous_trading_day(app_client):
    """★ 今日休市 ⇒ 参照日必须是**上一交易日**（用户原话：非交易日展示最近一个交易日）。"""
    d = _data(app_client)
    if not d["trading_day"]:
        assert d["last_trading_day"] != d["today"], d
        assert d["last_trading_day"] < d["today"], d
        # 参照日本身必须是真交易日
        ref = date(int(d["last_trading_day"][:4]), int(d["last_trading_day"][4:6]), int(d["last_trading_day"][6:8]))
        assert is_trading_day_exact(ref), d


def test_next_trading_day_is_strictly_after_today(app_client):
    """`next_trading_day` 严格晚于今天 —— 否则「下一交易日」会显示成今天，自相矛盾。"""
    d = _data(app_client)
    assert d["next_trading_day"] > d["today"], d


# ---------------- 日历函数语义 ----------------


def test_weekend_is_not_trading_day():
    # 2026-09-19 是周六，2026-09-20 是周日
    assert is_trading_day(date(2026, 9, 19)) is False
    assert is_trading_day(date(2026, 9, 20)) is False


def test_holiday_is_not_trading_day():
    # 2026-10-01 国庆（周四），内置表精确覆盖范围内
    assert is_trading_day(date(2026, 10, 1)) is False


def test_compensated_weekends_are_not_trading_days():
    """★ R26：调休补班日（周末上班日）**不是** A 股交易日。

    交易所 2026 年休市通知原文逐条写明这些周末日是「周末休市」：
    「另外，1 月 4 日（星期日）为周末休市」「10 月 10 日（星期六）为周末休市」…
    旧实现把它们放进 `WORKDAYS` 当作交易日，直接后果是 `/market/session` 在
    周末声称「今天是交易日」、引擎整天高频空转打源。
    """
    for iso in ("2026-01-04", "2026-02-14", "2026-02-15", "2026-04-05",
                "2026-09-27", "2026-10-10", "2025-01-26", "2025-09-28"):
        y, m, dd = (int(x) for x in iso.split("-"))
        assert is_trading_day(date(y, m, dd)) is False, iso


def test_missing_holiday_weekdays_are_not_trading_days():
    """★ R26：原内置表漏列的**工作日**休市日（春节/劳动节尾部）。

    2026 春节 2/15-2/23、劳动节 5/1-5/5、2024-2025 春节尾部，均含周一至周五；
    漏列 ⇒ `phase()` 返回 "closed"（应为 "holiday"）、`is_active()` 全天为真。
    """
    for iso in ("2026-02-23", "2026-05-04", "2026-05-05",
                "2025-01-28", "2025-01-29", "2025-01-30"):
        y, m, dd = (int(x) for x in iso.split("-"))
        assert is_trading_day(date(y, m, dd)) is False, iso


def test_next_trading_day_after_mid_autumn_is_monday():
    """★ R26：中秋 9/25(五)-9/27(日) 休市 ⇒ 下一交易日是 9/28（周一），不是周日。

    旧实现把 9/27（周日补班日）当交易日，`next_trading_day` 直接返回周日 ——
    界面上「下一交易日」指向一个不可能有行情的日期。
    """
    assert next_trading_day(date(2026, 9, 25)) == date(2026, 9, 28)
    assert prev_trading_day(date(2026, 9, 28), include_self=False) == date(2026, 9, 24)


def test_long_holiday_runs_use_real_exchange_schedule():
    """春节/劳动节长假边界（2026 官方安排）。"""
    assert next_trading_day(date(2026, 2, 13)) == date(2026, 2, 24)    # 春节 2/15-2/23
    assert prev_trading_day(date(2026, 2, 24), include_self=False) == date(2026, 2, 13)
    assert next_trading_day(date(2026, 4, 30)) == date(2026, 5, 6)     # 劳动 5/1-5/5
    assert next_trading_day(date(2026, 1, 2)) == date(2026, 1, 5)      # 元旦 1/1-1/3


def test_coverage_does_not_claim_unpublished_year():
    """★ R26：2027 年安排公布前不得声称「精确」。

    原 `_CALENDAR_MAX_YEAR = 2027` 但 2027 只填了元旦 1 天 ⇒ `exact=True` 是
    虚假精度，上层会据此把启发式结果当交易所精确日历用。
    """
    from app.sync.calendar import exchange_calendar

    assert exchange_calendar.coverage(date(2026, 12, 31)).exact is True
    assert exchange_calendar.coverage(date(2027, 6, 1)).exact is False


def test_snapshot_holiday_is_never_closed():
    """★ R26：工作日节假日必须是 holiday，不得是 closed。

    实测缺陷（2026-09-25 中秋，周五）：`/market/session` 返回
    `trading_day=False` 却 `phase="closed"` —— `phase` 取自 TradingSession 自己的
    判定，与权威的 `is_trading_day_exact` 各调一次，口径分叉即自相矛盾。
    界面据 `phase` 显示「已收盘」，而真相是「休市」，引导完全不同。
    """
    snap = session_snapshot(_at(date(2026, 9, 25), hour=23))
    assert snap["today"] == "20260925"
    assert snap["trading_day"] is False
    assert snap["phase"] == "holiday", snap


def test_snapshot_phase_obeys_authoritative_trading_day(monkeypatch):
    """★ R26：phase 必须服从权威交易日判定（结构性不变量，而非两个来源恰好一致）。"""
    import gateway.trading_session as ts_mod

    monkeypatch.setattr(ts_mod.TradingSession, "phase", lambda self, now=None: "closed")
    snap = session_snapshot(_at(date(2026, 9, 25), hour=11))   # 中秋休市
    assert snap["trading_day"] is False
    assert snap["phase"] == "holiday", snap


def test_prev_trading_day_skips_weekend():
    """周五之后回退是周四；周六之后回退是周五。"""
    assert prev_trading_day(date(2026, 9, 19), include_self=False) == date(2026, 9, 18)
    assert prev_trading_day(date(2026, 9, 18)) == date(2026, 9, 18)
    # 周日 → 上一交易日是周五（跨过周六）
    assert prev_trading_day(date(2026, 9, 20), include_self=False) == date(2026, 9, 18)


def test_prev_trading_day_skips_holiday_run():
    """国庆长假：10-08 回退必须跨过整段假期回到 09-30。"""
    assert prev_trading_day(date(2026, 10, 8), include_self=False) == date(2026, 9, 30)


def test_next_trading_day_skips_weekend_and_holiday():
    assert next_trading_day(date(2026, 9, 19)) == date(2026, 9, 21)
    assert next_trading_day(date(2026, 9, 30)) == date(2026, 10, 8)
    # include_self=True 时交易日返回自己
    assert next_trading_day(date(2026, 9, 18), include_self=True) == date(2026, 9, 18)


def test_prev_next_are_inverse_on_trading_days():
    """互为逆运算：交易日 d 的下一交易日的上一交易日就是 d。"""
    d = date(2026, 9, 18)
    assert prev_trading_day(next_trading_day(d), include_self=False) == d


def test_scan_bounded_no_infinite_loop():
    """扫描上限 30 天：即使传入极端年份也必须返回，不能死循环。"""
    far = date(1999, 1, 1)
    assert isinstance(prev_trading_day(far), date)
    assert isinstance(next_trading_day(far), date)


# ---------------- 权威判定优先级 ----------------


def test_exact_judgement_prefers_exchange_calendar(monkeypatch):
    """★ 覆盖区间**内**，券商日历必须压过内置表。

    真实场景：交易所临时休市/调休，内置节假日表不可能知道。若这里退回内置表，
    界面会声称「今天是交易日」，而实际上没有任何数据更新。
    """
    import gateway.trading_session as ts_mod

    # 区间 20260101..20260930 覆盖 09-18，但集合里没有 09-18：
    # 内置表说它是交易日，券商日历说不是 —— 区间内必须听券商的。
    fake = ts_mod.TradingSession(calendar=["20260101", "20260930"])
    monkeypatch.setattr(ts_mod, "default_session", fake)

    assert fake.has_calendar is True
    assert fake.covers(date(2026, 9, 18)) is True
    assert is_trading_day(date(2026, 9, 18)) is True          # 内置表口径
    assert is_trading_day_exact(date(2026, 9, 18)) is False   # 权威口径（券商）


def test_exact_judgement_ignores_calendar_outside_span(monkeypatch):
    """★ 覆盖区间**外**不得用「在不在集合里」判定（本轮修的核心缺陷）。

    实测 QMT 无参调用 `get_trading_calendar()` 返回 19901219..20260918
    —— 只到「最后一个已公布交易日」。若区间外照集合判定，2026-09-21（周一）
    会被判成休市，`next_trading_day` 于是走满 30 天扫描上限返回 20261020。
    """
    import gateway.trading_session as ts_mod

    fake = ts_mod.TradingSession(calendar=["20260101"])   # 区间仅 1 天
    monkeypatch.setattr(ts_mod, "default_session", fake)

    assert fake.covers(date(2026, 9, 18)) is False
    assert fake.is_trading_day(date(2026, 9, 18)) is True      # 区间外 → 内置表
    assert is_trading_day_exact(date(2026, 9, 18)) is True
    # 未来交易日同样必须能判出来
    assert is_trading_day_exact(date(2026, 9, 21)) is True
    assert next_trading_day(date(2026, 9, 19)) == date(2026, 9, 21)


def test_stale_calendar_does_not_idle_engines(monkeypatch):
    """★ 日历过期（跨周末后 max 仍是上周五）时，周一仍须判定为交易日。

    否则 `is_active()` 整天为 False ⇒ 引擎一整天不轮询、行情停更且零报错。
    这是「区间外判定」缺陷最严重的后果。
    """
    import gateway.trading_session as ts_mod

    # 模拟：客户端周五启动，日历区间到 20260918 为止，进程一直没重启
    fake = ts_mod.TradingSession(calendar=["20260917", "20260918"])
    monkeypatch.setattr(ts_mod, "default_session", fake)

    assert fake.calendar_span == ("20260917", "20260918")
    # 2026-09-21 是周一：区间外 → 内置表 → 交易日
    assert fake.is_trading_day(date(2026, 9, 21)) is True
    assert fake.is_active(_at(date(2026, 9, 21), hour=10)) is True


def test_exact_judgement_falls_back_to_builtin(monkeypatch):
    """未注入券商日历时退回内置节假日表（比「周一~周五」精确）。"""
    import gateway.trading_session as ts_mod

    fake = ts_mod.TradingSession()
    monkeypatch.setattr(ts_mod, "default_session", fake)

    assert fake.has_calendar is False
    assert is_trading_day_exact(date(2026, 10, 1)) is False   # 国庆
    assert is_trading_day_exact(date(2026, 9, 18)) is True    # 周五


def test_snapshot_never_points_as_of_to_future():
    """as_of 绝不允许指向未来：那意味着界面会把「还没发生的行情」当成已有数据。"""
    for probe in (date(2026, 9, 18), date(2026, 9, 19), date(2026, 10, 1), date(2026, 10, 8)):
        snap = session_snapshot(_at(probe))
        assert snap["as_of"] <= snap["today"], (probe, snap)
        assert snap["last_trading_day"] <= snap["today"], (probe, snap)


def test_snapshot_off_day_label_semantics():
    """周六快照：trading_day=False，参照日=周五，下一交易日=周一。"""
    snap = session_snapshot(_at(date(2026, 9, 19)))
    assert snap["today"] == "20260919"
    assert snap["trading_day"] is False
    assert snap["active"] is False
    assert snap["phase"] == "holiday"
    assert snap["last_trading_day"] == "20260918"
    assert snap["next_trading_day"] == "20260921"


def test_snapshot_trading_day_uses_today():
    """交易日快照：参照日=今天，phase 落在非 holiday 集合内。"""
    snap = session_snapshot(_at(date(2026, 9, 18), hour=10))
    assert snap["trading_day"] is True
    assert snap["last_trading_day"] == "20260918"
    assert snap["as_of"] == "20260918"
    assert snap["phase"] in PHASES and snap["phase"] != "holiday"


def _at(d: date, hour: int = 12):
    """构造指定日期的本地 datetime（session_snapshot 接受 now 参数便于测试）。"""
    from core.clock import local_now

    return local_now().replace(year=d.year, month=d.month, day=d.day,
                               hour=hour, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize("hour,expected", [
    (8, "pre_open"),
    (10, "open"),
    (12, "lunch_break"),
    (14, "open"),
    (16, "closed"),
])
def test_snapshot_phase_by_hour(hour, expected):
    """阶段划分：盘前 / 盘中 / 午休 / 盘中 / 已收盘 —— 五档必须都能区分出来。"""
    snap = session_snapshot(_at(date(2026, 9, 18), hour=hour))
    assert snap["phase"] == expected, snap
