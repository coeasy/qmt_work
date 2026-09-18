"""gateway/market_sync.py 核心路径测试（T26 补缺，此前 5102 行 0 测试）。

覆盖：启停生命周期 / 热窗口滚动搬移（按边界去重）/ enabled 门控 /
同一天只刷一次 / **过触发时刻启动即补跑** / 非交易日跳过 /
刷新批次并发与结果统计 / 回源根数按热窗口换算。
（项目约定：无 pytest-asyncio，顶层 asyncio.run 包裹。）
"""
import asyncio
from datetime import datetime as _datetime
from types import SimpleNamespace

from gateway.market_sync import MarketSync


def _dt(y, m, d, hh, mm):
    """构造 Asia/Shanghai 语义的 naive datetime（EOD 时区打桩点）。"""
    return _datetime(y, m, d, hh, mm)


class _Cfg:
    """runtime_config 桩：内存字典 + 覆盖。"""
    def __init__(self, **kv):
        self._kv = kv
    def get(self, key, default=None):
        parts = key.split(".")
        cur = self._kv
        for p in parts:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur


class _KC:
    """kline_cache 桩：记录 archive_rollover / hot_series 调用。

    ``hot_cutoff`` 可改：滚动搬移现在**按边界变化去重**（边界不变就跳过全表扫描），
    测试需要能推动边界来触发第二次搬移。
    """
    def __init__(self, series=None, cutoff="2026-06-17"):
        self._series = series or []
        self.hot_days = 92
        self._cutoff = cutoff
        self.rollover_calls = 0
        self.rollover_res = {"moved": 3, "deleted": 3}
        self.refresh_codes = []
    def hot_cutoff(self):
        return self._cutoff
    def archive_rollover(self):
        self.rollover_calls += 1
        return self.rollover_res
    def hot_series(self):
        return self._series


def _mk(series=None, **cfg):
    st = SimpleNamespace(kline_cache=_KC(series), _market_sync_last=None)
    rc = _Cfg(**cfg)
    ms = MarketSync(st, rc, interval=10)
    return ms, st, rc


def _run(coro):
    return asyncio.run(coro)


def test_start_runs_rollover_and_loop(monkeypatch):
    ms, st, _ = _mk()
    async def _never(): await asyncio.sleep(999)
    monkeypatch.setattr(ms, "_loop", _never)
    _run(ms.start())
    assert st.kline_cache.rollover_calls == 1       # 启动即做一次归档维护
    _run(ms.stop())


def test_stop_cancels_task(monkeypatch):
    ms, st, _ = _mk()
    async def _never2(): await asyncio.sleep(999)
    monkeypatch.setattr(ms, "_loop", _never2)
    _run(ms.start())
    assert ms._task is not None
    _run(ms.stop())
    assert ms._task is None


def test_rollover_no_cache_returns_zero():
    ms = MarketSync(SimpleNamespace(kline_cache=None), _Cfg(), interval=10)
    res = _run(ms._rollover())
    assert res == {"moved": 0, "deleted": 0}


def test_rollover_exception_swallowed():
    kc = _KC()
    def _boom():
        raise RuntimeError("db locked")
    kc.archive_rollover = _boom
    ms = MarketSync(SimpleNamespace(kline_cache=kc), _Cfg(), interval=10)
    res = _run(ms._rollover())
    assert res == {"moved": 0, "deleted": 0}        # 失败静默降级，不炸循环


def test_tick_disabled_skips_refresh(monkeypatch):
    ms, st, rc = _mk(market={"sync": {"enabled": False}})
    async def _boom2(): raise AssertionError("不应调用")
    monkeypatch.setattr(ms, "_refresh_hot", _boom2)
    _run(ms._tick())
    assert st._market_sync_last is None


def test_tick_same_day_only_once(monkeypatch):
    ms, st, _ = _mk(market={"sync": {"enabled": True, "time": "00:00"}})
    async def _noop(): return None
    monkeypatch.setattr(ms, "_refresh_hot", _noop)
    monkeypatch.setattr("gateway.market_sync._sh_now",
                        lambda: _dt(2026, 8, 31, 9, 0))
    monkeypatch.setattr("gateway.trading_session.default_session.is_trading_day",
                        lambda: True, raising=False)
    _run(ms._tick())
    assert ms._last_run_date == "2026-08-31"        # 今天已刷
    _run(ms._tick())
    # 第二次 tick：当天不重刷；滚动搬移也因**热窗口边界未变**而跳过
    # （原实现每个 tick 都全表重扫热表，一天 1440 次无意义全扫）
    assert st.kline_cache.rollover_calls == 1


def test_tick_after_sync_time_catches_up(monkeypatch):
    """★ 启动时已过触发时刻且当天未同步 ⇒ 立即补跑。

    用户原话：「超过16点未同步，启动之后继续检查同步」。判据就是
    「开关开 + 今天没跑过 + 触发时刻已过」——「已过」天然包含「启动时已过」，
    不需要单开一个补跑分支（两套判据迟早漂移）。
    """
    ms, st, _ = _mk(market={"sync": {"enabled": True, "time": "16:00"}})
    calls = []

    async def _rec():
        calls.append(1)

    monkeypatch.setattr(ms, "_refresh_hot", _rec)
    monkeypatch.setattr("gateway.market_sync._sh_now",
                        lambda: _dt(2026, 8, 31, 17, 30))
    monkeypatch.setattr("gateway.trading_session.default_session.is_trading_day",
                        lambda: True, raising=False)
    _run(ms._tick())
    assert calls == [1], "过了 16:00 且当天未同步，必须立即补跑"
    assert ms._last_run_date == "2026-08-31"


def test_tick_non_trading_day_skips(monkeypatch):
    """非交易日即使过了触发时刻也不补跑（用券商真实日历判定，不认周末启发式）。"""
    ms, st, _ = _mk(market={"sync": {"enabled": True, "time": "16:00"}})

    async def _boom3():
        raise AssertionError("非交易日不应刷新")

    monkeypatch.setattr(ms, "_refresh_hot", _boom3)
    monkeypatch.setattr("gateway.market_sync._sh_now",
                        lambda: _dt(2026, 8, 30, 17, 0))
    monkeypatch.setattr("gateway.trading_session.default_session.is_trading_day",
                        lambda: False, raising=False)
    _run(ms._tick())
    assert ms._last_run_date is None


def test_rollover_deduped_by_cutoff():
    """滚动搬移按热窗口边界去重：边界不变只跑一次，边界前进立刻再跑。"""
    ms, st, _ = _mk()
    _run(ms._rollover())
    _run(ms._rollover())
    assert st.kline_cache.rollover_calls == 1
    st.kline_cache._cutoff = "2026-06-18"           # 边界前进一天
    _run(ms._rollover())
    assert st.kline_cache.rollover_calls == 2


def test_apply_hot_days_hot_update():
    """market.hot_days 改动无需重启即生效（磁盘紧张时可随时调小热窗口）。"""
    ms, st, _ = _mk(market={"hot_days": 30})
    ms._apply_hot_days()
    assert st.kline_cache.hot_days == 30


def test_hot_fetch_count_tracks_window():
    """回源根数按热窗口换算：不能少取（92 根 ≈ 4.5 个月），也不能多取（250 根 ≈ 1 年）。

    固定 250 根是改造前的行为 —— 它会把窗口外的历史天天重拉一遍并重写冷仓，
    正是「只更新热数据」要消掉的开销。
    """
    from gateway.market_sync import _HOT_FETCH_MARGIN, hot_fetch_count
    assert hot_fetch_count(92) == int(92 * 250 / 365) + _HOT_FETCH_MARGIN
    assert hot_fetch_count(92) < 250
    assert hot_fetch_count(365) > 250
    assert hot_fetch_count(0) == hot_fetch_count(1) > 0   # 非法值兜底，不返回 0 根


def test_tick_before_sync_time_skips(monkeypatch):
    ms, st, _ = _mk(market={"sync": {"enabled": True, "time": "16:00"}})
    async def _boom2(): raise AssertionError("不应调用")
    monkeypatch.setattr(ms, "_refresh_hot", _boom2)
    monkeypatch.setattr("gateway.market_sync._sh_now",
                        lambda: _dt(2026, 8, 31, 9, 0))
    _run(ms._tick())
    assert ms._last_run_date is None


def test_refresh_hot_batch_and_stats(monkeypatch):
    series = [{"code": f"600{100+i:03d}.SH", "period": "1d"} for i in range(5)]
    ms, st, _ = _mk(series=series, market={"sync": {"enabled": True}})
    asked = []

    async def _fake_fetch(code, period, count, force=False):
        asked.append((code, count))
        if code.endswith("102.SH"):
            raise RuntimeError("boom")
        st.kline_cache.refresh_codes.append(code)
    monkeypatch.setattr("tools.fetch_kline_cached", _fake_fetch)
    _run(ms._refresh_hot())
    assert len(st.kline_cache.refresh_codes) == 4    # 5 - 1 失败
    assert st._market_sync_last["codes"] == 5
    assert st._market_sync_last["ok"] == 4
    assert st._market_sync_last["fail"] == 1
    # ★ 「只更新热数据」的可观测判据：回源根数按热窗口算，不是写死的 250
    from gateway.market_sync import hot_fetch_count
    assert {c for _, c in asked} == {hot_fetch_count(92)}
    assert st._market_sync_last["count_per_code"] == hot_fetch_count(92)


def test_refresh_hot_filters_periods(monkeypatch):
    series = [{"code": "A", "period": "1d"}, {"code": "B", "period": "5m"},
              {"code": "C", "period": "1w"}, {"code": "D", "period": "tick"}]
    ms, st, _ = _mk(series=series, market={"sync": {"enabled": True}})
    monkeypatch.setattr("tools.fetch_kline_cached",
                        lambda *a, **k: None)
    _run(ms._refresh_hot())
    assert st._market_sync_last["codes"] == 2        # 仅 1d/1w 进入刷新集
