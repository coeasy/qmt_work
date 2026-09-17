"""`GET /api/v1/health` 的 `trading_session` 契约。

为什么单独立契约：前端状态栏要区分**今日休市**（节假日/周末）与**非交易时段**
（盘前盘后/午休）。只看 `active` 会把两者混成同一件事 —— 因为 `active=false`
恰好是两者共有的值 —— 用户看到的是「今天该开盘却没开」却找不到原因。
区分它们需要 `trading_day`，而**前端没有券商交易日历**（`shared/format.ts::isTradingHours`
只是「周一~周五 + 时段」的本地近似，国庆节周三会判成交易时段）。
所以这个字段必须由后端给，且必须一直给 —— 缺了它前端只会静默退回本地估算，
表现为「角标看着正常但节假日判断是错的」，属于最不容易被发现的那类回归。
"""

from __future__ import annotations


def _trading_session(app_client) -> dict:
    r = app_client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("code") == 0, body
    ts = (body.get("data") or {}).get("trading_session")
    assert isinstance(ts, dict), f"health 响应缺少 trading_session：{body}"
    return ts


def test_health_exposes_trading_day(app_client):
    """三个键必须同时在：mode / active / trading_day。"""
    ts = _trading_session(app_client)
    assert set(ts) >= {"mode", "active", "trading_day"}, ts
    assert isinstance(ts["mode"], str) and ts["mode"], ts
    for key in ("active", "trading_day"):
        # 允许 None（交易日历未加载完），但不允许是别的类型 ——
        # 前端按 typeof === "boolean" 判定，类型一变就静默降级成 null。
        assert ts[key] is None or isinstance(ts[key], bool), (key, ts)


def test_active_implies_trading_day(app_client):
    """不变量：正在盘中 ⇒ 今天必然是交易日。

    依据 `TradingSession.is_active = is_trading_day(今天) and in_active_hours(now)`。
    这条不变量是前端「今日休市」判定的前提：若 active=True 而 trading_day=False，
    前端会同时收到「在盘中」和「今天休市」两个矛盾信号。
    """
    ts = _trading_session(app_client)
    if ts["active"] is True:
        assert ts["trading_day"] is True, ts


class _StubSession:
    """把「交易日 / 盘中」两个维度拆开注入 —— 真实时钟做不到（同一时刻只有一个值）。"""

    def __init__(self, *, active: bool, trading_day: bool) -> None:
        self._active = active
        self._day = trading_day

    def stats(self) -> dict:
        return {"mode": "stub", "days": 0, "active_now": self._active}

    def is_active(self) -> bool:
        return self._active

    def is_trading_day(self) -> bool:
        return self._day


def test_after_hours_on_a_trading_day(app_client, monkeypatch):
    """★ 交易日盘后：active=False 但 trading_day=True —— 两个值必须都被带上。"""
    import gateway.trading_session as mod

    monkeypatch.setattr(mod, "default_session",
                        _StubSession(active=False, trading_day=True))
    ts = _trading_session(app_client)
    assert ts["active"] is False
    assert ts["trading_day"] is True, ts
    assert ts["mode"] == "stub"


def test_holiday_is_distinguishable_from_after_hours(app_client, monkeypatch):
    """★ 两种 `active=False` 必须能被区分开 —— 这正是本轮修的那个问题。

    若实现退化成 `trading_day = is_active()`，本用例与上一个用例会同时失败，
    而「国庆节显示交易时段 / 盘后显示今日休市」这类错判就再也无人预警。
    """
    import gateway.trading_session as mod

    monkeypatch.setattr(mod, "default_session",
                        _StubSession(active=False, trading_day=False))
    ts = _trading_session(app_client)
    assert ts["active"] is False
    assert ts["trading_day"] is False, ts


def test_calendar_unavailable_degrades_to_null_not_false(app_client, monkeypatch):
    """日历未就绪时应能表达「不知道」—— 不能把未知硬编码成 False（= 今日休市）。

    前端对 `trading_day === false` 的处置是「今日休市」，把未知当 False 会
    在日历尚未加载完时谎报休市。
    """
    import gateway.trading_session as mod

    class _Boom(_StubSession):
        def is_trading_day(self) -> bool:
            raise RuntimeError("calendar not ready")

    monkeypatch.setattr(mod, "default_session", _Boom(active=True, trading_day=True))
    ts = _trading_session(app_client)
    # 取不到交易日历时，整个 trading 块退化为 unknown/None，而不是半个真值
    assert ts == {"mode": "unknown", "active": None, "trading_day": None}, ts
