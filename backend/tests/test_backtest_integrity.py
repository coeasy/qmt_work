"""回测诚信契约测试（G1 降级标注 + 0 成交可解释）。

两条底线：
  1. 用缓存/降级缓存跑出的结果，必须带 stale + as_of —— 不能让用户把上周的
     缓存结论当成今天的实盘依据。
  2. 0 成交必须说清是「策略没信号」还是「本金买不起一手」——沉默的 0 会被
     误读成「策略不赚钱」，而真实原因可能是根本没进场。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest import (  # noqa: E402
    _annotate_no_trade,
    _annotate_provenance,
    run_backtest_engine,
)


def _mk_bars(prices, dates=None):
    n = len(prices)
    dates = dates or [f"2026-01-{i + 1:02d}" for i in range(n)]
    return [{"date": dates[i], "open": prices[i], "high": prices[i] * 1.01,
             "low": prices[i] * 0.99, "close": prices[i], "volume": 1_000_000}
            for i in range(n)]


def test_cache_stale_is_marked_with_as_of():
    out = {}
    bars = _mk_bars([10.0] * 5)
    _annotate_provenance(out, bars, {"source": "cache_stale", "cached_at": 1.0,
                                     "note": "券商取数失败，回退到本地历史缓存"})
    assert out["stale"] is True
    assert out["as_of"] == "2026-01-05"          # 最后一根 K 线的日期
    assert "回退到本地历史缓存" in out["data_source"]["stale_reason"]


def test_broker_source_is_not_marked_stale():
    out = {}
    _annotate_provenance(out, _mk_bars([10.0] * 3), {"source": "broker"})
    assert "stale" not in out
    assert "as_of" not in out
    assert out["data_source"]["source"] == "broker"


def test_no_meta_leaves_result_untouched():
    out = {}
    _annotate_provenance(out, _mk_bars([10.0] * 3), None)
    assert out == {}


def test_no_signal_is_reported_explicitly():
    out = {}
    # sig 全 0：策略从未发出持仓信号
    _annotate_no_trade(out, [10.0] * 5, [0] * 5, _cfg(), 100_000.0, [])
    assert "未产生任何持仓信号" in out["diagnostics"]


def test_insufficient_capital_is_reported_not_silently_zero():
    out = {}
    closes = [1400.0] * 50
    sig = [0] * 10 + [1] * 40
    _annotate_no_trade(out, closes, sig, _cfg(), 100_000.0, [])
    # 1 手 = 1400 × 100 = 14 万 > 10 万本金
    assert "初始资金不足" in out["diagnostics"]
    assert "1400.00" in out["diagnostics"]


def test_trades_present_means_no_diagnostics():
    out = {}
    _annotate_no_trade(out, [10.0] * 5, [1] * 5, _cfg(), 100_000.0,
                       [{"side": "buy", "pnl": 0}])
    assert "diagnostics" not in out


def test_engine_end_to_end_reports_unaffordable_symbol():
    """端到端：茅台价位的标的 + 10 万本金 -> 0 成交，且必须给出可操作的原因。"""
    # 先跌后涨再跌，保证 MA5/MA20 至少交叉一次（确实产生持仓信号）
    prices = [1500 - i * 8 for i in range(30)] + [1260 + i * 9 for i in range(30)] \
        + [1530 - i * 7 for i in range(60)]
    bars = _mk_bars(prices, [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(120)])
    res = run_backtest_engine("600519.SH", bars, "ma_cross", {"fast": 5, "slow": 20},
                              100_000.0)
    assert res["trade_count"] == 0
    assert "初始资金不足" in res["diagnostics"]
    # 指标仍如实输出，不因无法交易而伪造
    assert res["metrics"]["total_return"] == 0.0


def test_engine_affordable_capital_does_trade():
    """同标的提高本金到 30 万 -> 应当正常建仓成交，且不再有 diagnostics。"""
    prices = [1500 - i * 8 for i in range(30)] + [1260 + i * 9 for i in range(30)] \
        + [1530 - i * 7 for i in range(60)]
    bars = _mk_bars(prices, [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(120)])
    res = run_backtest_engine("600519.SH", bars, "ma_cross", {"fast": 5, "slow": 20},
                              300_000.0)
    assert res["trade_count"] > 0
    assert "diagnostics" not in res


def test_equity_curve_length_matches_kline():
    """净值曲线必须与 K 线逐根对齐（缺帧会让所有指标建立在错位序列上）。"""
    prices = [10 + (i % 7) for i in range(120)]
    bars = _mk_bars(prices)
    res = run_backtest_engine("000001.SZ", bars, "ma_cross", {"fast": 5, "slow": 20},
                              100_000.0)
    assert len(res["equity_curve"]) == len(bars)


def _cfg():
    from tools.matching import MatchingConfig
    return MatchingConfig(code="600519.SH")
