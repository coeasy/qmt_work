"""经典选股策略（复刻 Sequoia-X）回归测试。

锁的是**形态判据**本身：每个策略给一组「应当命中」与「应当不命中」的 K 线，
防止后续改动把判据悄悄放宽/收紧（例如把「突破前 N 日高点」退化成「含当日」）。
"""
from __future__ import annotations

import pytest

from app.screener.classic import (
    CLASSIC_STRATEGIES,
    STRATEGY_IDS,
    compute_rps,
    evaluate_classic,
    period_return,
    run_classic,
    strategy_meta,
)


class Bar:
    """最小 Bar 替身（duck-typed，与真实 Bar 字段一致）。"""

    def __init__(self, close, open_=None, high=None, low=None,
                 volume=1e6, amount=None):
        self.close = close
        self.open = open_ if open_ is not None else close
        self.high = high if high is not None else max(self.open, close)
        self.low = low if low is not None else min(self.open, close)
        self.volume = volume
        self.amount = amount


def _bars(closes, **kw) -> list[Bar]:
    """由收盘价序列构造 Bar（默认平开、高低各留 1% 余量）。"""
    out = []
    for c in closes:
        out.append(Bar(close=c, open_=c, high=c * 1.01, low=c * 0.99,
                       volume=kw.get("volume", 1e6), amount=kw.get("amount")))
    return out


# ---------------- 注册表 ----------------

def test_all_strategies_registered():
    assert set(STRATEGY_IDS) == {
        "turtle_trade", "ma_volume", "high_tight_flag",
        "limit_up_shakeout", "uptrend_limit_down", "rps_breakout"}
    for sid in STRATEGY_IDS:
        meta = strategy_meta(sid)
        assert meta and meta["id"] == sid and meta["label"]
        assert isinstance(meta["params"], dict)


def test_unknown_strategy_is_false_not_raise():
    """★未知策略 / 空数据都返回 False，绝不抛异常（全市场扫描不能被单只中断）。"""
    assert evaluate_classic(_bars([10, 11]), "does-not-exist")[0] is False
    assert evaluate_classic([], "turtle_trade")[0] is False
    ok, detail = evaluate_classic([], "turtle_trade")
    assert ok is False and "reason" in detail


# ---------------- 海龟突破 ----------------

def test_turtle_trade_hits():
    """前 20 日高点 11，今日收 12 且成交额过亿、收阳 → 命中。"""
    bars = _bars([10.0] * 25, amount=1e6)
    bars[-1] = Bar(close=12.0, open_=11.0, high=12.1, low=10.9,
                   volume=1e6, amount=2e8)
    ok, d = evaluate_classic(bars, "turtle_trade")
    assert ok is True, d
    assert d["amount_ok"] is True and d["yang_line"] is True


def test_turtle_trade_rejects_low_amount():
    """成交额不足 → 不命中（防流动性陷阱）。"""
    bars = _bars([10.0] * 25)
    bars[-1] = Bar(close=12.0, open_=11.0, high=12.1, low=10.9,
                   volume=1e6, amount=1e7)  # 仅 1 千万
    assert evaluate_classic(bars, "turtle_trade")[0] is False


def test_turtle_trade_rejects_yin_line():
    """阴线（收 < 开）→ 不命中，防诱多。"""
    bars = _bars([10.0] * 25)
    bars[-1] = Bar(close=12.0, open_=12.5, high=12.6, low=11.9,
                   volume=1e6, amount=2e8)
    ok, d = evaluate_classic(bars, "turtle_trade")
    assert ok is False and d["yang_line"] is False


def test_turtle_trade_rejects_not_breakout():
    """未突破前 20 日高点 → 不命中。"""
    bars = _bars([10.0] * 20 + [15.0] * 4, amount=2e8)  # 前高 ~15.15
    bars[-1] = Bar(close=12.0, open_=11.0, high=12.1, low=10.9,
                   volume=1e6, amount=2e8)
    assert evaluate_classic(bars, "turtle_trade")[0] is False


# ---------------- 均线放量 ----------------

def test_ma_volume_hits():
    """站上 MA20 + 短均多头 + 放量 → 命中。"""
    closes = [10.0] * 19 + [10.5] * 5 + [12.0]
    bars = _bars(closes, volume=1e6)
    bars[-1].volume = 5e6      # 放量 5 倍
    ok, d = evaluate_classic(bars, "ma_volume")
    assert ok is True, d
    assert d["above_ma"] and d["golden_cross"] and d["volume_surge"]


def test_ma_volume_rejects_no_surge():
    """量能未放大 → 不命中。"""
    closes = [10.0] * 19 + [10.5] * 5 + [12.0]
    bars = _bars(closes, volume=1e6)   # 全程等量
    assert evaluate_classic(bars, "ma_volume")[0] is False


# ---------------- 高窄旗形 ----------------

def test_high_tight_flag_hits():
    """前期大涨 → 窄幅整理 → 突破整理高点 → 命中。"""
    # 前期：60 根从 10 涨到 14（+40%）
    prior = [10.0 + i * (4.0 / 59) for i in range(60)]
    # 旗形：10 根窄幅横盘（振幅 < 15%）
    flag = [14.0, 14.05, 13.95, 14.02, 14.0, 14.03, 13.98, 14.01, 14.0, 14.02]
    bars = _bars(prior + flag)
    bars[-1] = Bar(close=15.0, open_=14.0, high=15.1, low=14.0)  # 突破
    ok, d = evaluate_classic(bars, "high_tight_flag",
                             {"lookback": 60, "flag_days": 10})
    assert ok is True, d


def test_high_tight_flag_rejects_wide_flag():
    """整理区过宽（非旗形）→ 不命中。"""
    prior = [10.0 + i * (4.0 / 59) for i in range(60)]
    flag = [14.0, 10.0, 14.0, 10.0, 14.0, 10.0, 14.0, 10.0, 14.0, 10.0]  # 剧烈震荡
    bars = _bars(prior + flag)
    bars[-1] = Bar(close=15.0, open_=14.0, high=15.1, low=14.0)
    assert evaluate_classic(bars, "high_tight_flag",
                            {"lookback": 60, "flag_days": 10})[0] is False


# ---------------- 涨停洗盘 ----------------

def test_limit_up_shakeout_hits():
    """涨停 → 回踩不破涨停日最低 → 再度转强 → 命中。"""
    bars = _bars([10.0] * 8)
    # 第 8 根涨停（+10%）
    bars.append(Bar(close=11.0, open_=10.0, high=11.0, low=10.2))   # +10%
    # 回踩两根，最低 10.5（未破 10.2）
    bars.append(Bar(close=10.7, open_=10.9, high=10.9, low=10.5))
    bars.append(Bar(close=10.6, open_=10.7, high=10.8, low=10.5))
    # 转强：收阳且高于前收
    bars.append(Bar(close=11.2, open_=10.6, high=11.3, low=10.6))
    ok, d = evaluate_classic(bars, "limit_up_shakeout", {"window": 10})
    assert ok is True, d
    assert d["hold_support"] is True and d["recover"] is True


def test_limit_up_shakeout_rejects_broken_support():
    """回踩跌破涨停日最低价 → 不命中（破位，不再是洗盘）。"""
    bars = _bars([10.0] * 8)
    bars.append(Bar(close=11.0, open_=10.0, high=11.0, low=10.2))   # 涨停
    bars.append(Bar(close=9.5, open_=10.5, high=10.6, low=9.4))     # 跌破 10.2
    bars.append(Bar(close=10.0, open_=9.5, high=10.1, low=9.5))
    bars.append(Bar(close=10.5, open_=10.0, high=10.6, low=10.0))
    ok, d = evaluate_classic(bars, "limit_up_shakeout", {"window": 10})
    assert ok is False and d["hold_support"] is False


# ---------------- 跌停反包 ----------------

def test_uptrend_limit_down_hits():
    """均线之上且上行 + 昨日跌停 + 今日吞没昨日最高 → 命中。"""
    closes = [10.0 + i * 0.15 for i in range(30)]     # 稳定上行
    bars = _bars(closes)
    # ★ 跌停是相对「前一根」算的：基准必须取 bars[-2]（跌停日的前收），
    # 取 bars[-1] 会因最后一根本身涨过而只有约 -9%，反而测不到跌停分支。
    ref = bars[-2].close
    bars[-1] = Bar(close=ref * 0.90, open_=ref * 0.95,
                   high=ref * 0.96, low=ref * 0.89)   # 昨日跌停 -10%
    bars.append(Bar(close=ref * 1.00, open_=ref * 0.90,
                    high=ref * 1.01, low=ref * 0.89))  # 今日反包
    ok, d = evaluate_classic(bars, "uptrend_limit_down")
    assert ok is True, d
    assert d["had_limit_down"] and d["engulf"] and d["uptrend"]


def test_uptrend_limit_down_rejects_no_engulf():
    """未吞没昨日最高价 → 不命中。"""
    closes = [10.0 + i * 0.15 for i in range(30)]
    bars = _bars(closes)
    ref = bars[-2].close
    bars[-1] = Bar(close=ref * 0.90, open_=ref * 0.95,
                   high=ref * 0.96, low=ref * 0.89)   # 昨日确实跌停
    # 今日未吞没昨日最高价（0.92 < 0.96）⇒ 应因「未反包」而不命中，
    # 而不是因为没识别出跌停 —— 这样测才有区分度。
    bars.append(Bar(close=ref * 0.92, open_=ref * 0.90,
                    high=ref * 0.93, low=ref * 0.89))
    ok, d = evaluate_classic(bars, "uptrend_limit_down")
    assert ok is False
    assert d["had_limit_down"] is True and d["engulf"] is False


# ---------------- RPS（横截面） ----------------

def test_compute_rps_percentile():
    """RPS 是百分位：最强 100，最弱 0。"""
    rps = compute_rps({"A": 1.0, "B": 5.0, "C": 3.0})
    assert rps["A"] == 0.0 and rps["C"] == 50.0 and rps["B"] == 100.0


def test_compute_rps_edge_cases():
    assert compute_rps({}) == {}
    assert compute_rps({"A": None}) == {}
    assert compute_rps({"A": 1.0}) == {"A": 100.0}   # 单一标的无参照系


def test_rps_breakout_needs_pool_rps():
    """★RPS 是横截面指标：不给 rps 就绝不命中（单看 K 线算不出来）。"""
    bars = _bars([10.0] * 25)
    bars[-1] = Bar(close=12.0, open_=11.0, high=12.1, low=10.9)
    assert evaluate_classic(bars, "rps_breakout")[0] is False
    ok, _ = evaluate_classic(bars, "rps_breakout", rps=95.0)
    assert ok is True


def test_rps_breakout_rejects_low_rps():
    bars = _bars([10.0] * 25)
    bars[-1] = Bar(close=12.0, open_=11.0, high=12.1, low=10.9)
    assert evaluate_classic(bars, "rps_breakout", rps=50.0)[0] is False


# ---------------- period_return / 批量 ----------------

def test_period_return():
    bars = _bars([10.0, 11.0, 12.0])
    assert period_return(bars, 1) == pytest.approx(9.0909, abs=1e-3)
    assert period_return(bars, 2) == pytest.approx(20.0, abs=1e-6)
    assert period_return(bars, 10) is None      # 数据不足返 None，不猜


def test_run_classic_computes_rps_from_pool():
    """run_classic 内部先全池排名再逐只判断（两步走）。"""
    strong = _bars([10.0 + i * 0.3 for i in range(30)])
    strong[-1] = Bar(close=strong[-1].close * 1.05, open_=strong[-1].close,
                     high=strong[-1].close * 1.06, low=strong[-1].close)
    weak = _bars([10.0 - i * 0.05 for i in range(30)])
    weak[-1] = Bar(close=weak[-1].close * 0.99, open_=weak[-1].close,
                   high=weak[-1].close, low=weak[-1].close * 0.98)
    out = run_classic({"STRONG": strong, "WEAK": weak}, "rps_breakout",
                      {"min_rps": 50.0, "rps_days": 10})
    codes = [r["code"] for r in out]
    assert "STRONG" in codes
    assert "WEAK" not in codes


def test_run_classic_unknown_strategy_returns_empty():
    assert run_classic({"A": _bars([1.0, 2.0])}, "nope") == []


def test_run_classic_rows_are_shape_compatible():
    """★结果必须带 close/change_pct：引擎按 _SORT_KEYS 排序、界面也读这两个字段，
    缺了会导致经典策略结果无法排序、界面显示空白。"""
    bars = _bars([10.0] * 25, amount=2e8)
    bars[-1] = Bar(close=12.0, open_=11.0, high=12.1, low=10.9, amount=2e8)
    out = run_classic({"A": bars}, "turtle_trade")
    assert out and out[0]["code"] == "A"
    assert out[0]["close"] == 12.0
    assert out[0]["change_pct"] is not None
    assert out[0]["amount_ok"] is True
