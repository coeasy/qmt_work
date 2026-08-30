"""gateway/market_sync.py 核心路径测试（T26 补缺，此前 5102 行 0 测试）。

覆盖：启停生命周期 / 跨年归档幂等 / enabled 门控 / 同一天只刷一次 /
非交易日跳过 / 刷新批次并发与结果统计。
（项目约定：无 pytest-asyncio，顶层 asyncio.run 包裹。）
"""
import asyncio
from types import SimpleNamespace

from gateway.market_sync import MarketSync


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
    """kline_cache 桩：记录 archive_rollover / all_series 调用。"""
    def __init__(self, series=None):
        self._series = series or []
        self.rollover_calls = 0
        self.rollover_res = {"moved": 3, "deleted": 3}
        self.refresh_codes = []
    def archive_rollover(self):
        self.rollover_calls += 1
        return self.rollover_res
    def all_series(self):
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
    monkeypatch.setattr("gateway.market_sync.time.strftime",
                        lambda fmt: "2026-08-31" if fmt == "%Y-%m-%d" else "09:00")
    monkeypatch.setattr("gateway.trading_session.default_session.is_trading_day",
                        lambda: True, raising=False)
    _run(ms._tick())
    assert ms._last_run_date == "2026-08-31"        # 今天已刷
    _run(ms._tick())
    assert st.kline_cache.rollover_calls == 2       # 二次只归档不重刷


def test_tick_before_sync_time_skips(monkeypatch):
    ms, st, _ = _mk(market={"sync": {"enabled": True, "time": "16:00"}})
    async def _boom2(): raise AssertionError("不应调用")
    monkeypatch.setattr(ms, "_refresh_hot", _boom2)
    monkeypatch.setattr("gateway.market_sync.time.strftime", lambda fmt: "09:00")
    _run(ms._tick())
    assert ms._last_run_date is None


def test_refresh_hot_batch_and_stats(monkeypatch):
    series = [{"code": f"600{100+i:03d}.SH", "period": "1d"} for i in range(5)]
    ms, st, _ = _mk(series=series, market={"sync": {"enabled": True}})

    async def _fake_fetch(code, period, count, force=False):
        if code.endswith("102.SH"):
            raise RuntimeError("boom")
        st.kline_cache.refresh_codes.append(code)
    monkeypatch.setattr("tools.fetch_kline_cached", _fake_fetch)
    _run(ms._refresh_hot())
    assert len(st.kline_cache.refresh_codes) == 4    # 5 - 1 失败
    assert st._market_sync_last["codes"] == 5
    assert st._market_sync_last["ok"] == 4
    assert st._market_sync_last["fail"] == 1


def test_refresh_hot_filters_periods(monkeypatch):
    series = [{"code": "A", "period": "1d"}, {"code": "B", "period": "5m"},
              {"code": "C", "period": "1w"}, {"code": "D", "period": "tick"}]
    ms, st, _ = _mk(series=series, market={"sync": {"enabled": True}})
    monkeypatch.setattr("tools.fetch_kline_cached",
                        lambda *a, **k: None)
    _run(ms._refresh_hot())
    assert st._market_sync_last["codes"] == 2        # 仅 1d/1w 进入刷新集
