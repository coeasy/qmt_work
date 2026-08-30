"""绩效指标专项测试（G10）。

锁定「不伪造数据」的底线：样本不足 / 无亏损交易时给出 None 而不是 0，
以及最大回撤必须能定位到具体发生在哪一段时间。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.metrics import compute_metrics, _month_key, _monthly_returns  # noqa: E402


# 先涨到 130，再跌到 80（最大回撤 -38.46%），最后修复并创新高
_EQUITY = [100, 110, 120, 130, 90, 80, 95, 115, 131, 140]
_DATES = ["2026-0%d-01" % i for i in range(1, 8)] + ["2026-08-01", "2026-09-01", "2026-10-01"]
_TRADES = [{"pnl": 20}, {"pnl": -15}, {"pnl": 8}, {"pnl": -4}]


def test_drawdown_window_located_by_date():
    m = compute_metrics(_EQUITY, _TRADES, period="1d", dates=_DATES)
    # 峰值 130 出现在 index 3，谷底 80 在 index 5，index 8(131) 首次回到峰值之上
    assert m["max_drawdown"] == -0.3846
    assert m["max_drawdown_start"] == "2026-04-01"
    assert m["max_drawdown_end"] == "2026-06-01"
    assert m["max_drawdown_recovery"] == "2026-09-01"
    assert m["max_drawdown_recovered"] is True
    assert m["max_drawdown_bars"] == 2


def test_drawdown_window_falls_back_to_index_without_dates():
    m = compute_metrics(_EQUITY, _TRADES, period="1d")
    assert (m["max_drawdown_start_idx"], m["max_drawdown_end_idx"]) == (3, 5)
    assert m["max_drawdown_recovery_idx"] == 8
    # 没有日期就绝不臆造月度分布
    assert "monthly_returns" not in m


def test_unrecovered_drawdown_is_none_not_zero():
    # 跌下去再也回不来
    eq = [100, 120, 60, 70, 80]
    m = compute_metrics(eq, period="1d", dates=["2026-01-01", "2026-02-01", "2026-03-01",
                                                "2026-04-01", "2026-05-01"])
    assert m["max_drawdown_recovered"] is False
    assert m["max_drawdown_recovery"] is None


def test_tail_metrics_absent_when_sample_too_small():
    m = compute_metrics(_EQUITY, period="1d")   # n=9 < 20
    assert m["var95"] is None
    assert m["cvar95"] is None
    assert "tail_metrics_note" in m


def test_tail_metrics_present_with_enough_samples():
    # 240 个观测，尾部 5% 必须能被算出来（正数表示损失幅度）
    eq = [100.0]
    for i in range(1, 241):
        eq.append(eq[-1] * (1.0 + (0.02 if i % 2 else -0.015)))
    m = compute_metrics(eq, period="1d")
    assert m["var95"] is not None and m["var95"] > 0
    assert m["cvar95"] is not None and m["cvar95"] >= m["var95"]   # CVaR 是尾部均值，不轻于 VaR
    assert "tail_metrics_note" not in m


def test_profit_factor_none_without_losing_trades():
    m = compute_metrics(_EQUITY, [{"pnl": 5}, {"pnl": 3}], period="1d")
    assert m["profit_factor"] is None
    assert m["payoff_ratio"] is None


def test_profit_factor_and_payoff_ratio():
    m = compute_metrics(_EQUITY, _TRADES, period="1d")
    assert m["profit_factor"] == round(28 / 19, 3)     # 总盈 28 / 总亏 19
    assert m["payoff_ratio"] == round(14 / 9.5, 3)     # 均盈 14 / 均亏 9.5
    assert m["avg_win"] == 14
    assert m["avg_loss"] == 9.5
    assert m["win_rate"] == 0.5


def test_sortino_penalizes_only_downside():
    # 上行波动大、下行波动小 -> Sortino 应显著优于 Sharpe
    eq = [100.0]
    for i in range(40):
        eq.append(eq[-1] * (1.03 if i % 2 == 0 else 0.99))
    m = compute_metrics(eq, period="1d")
    assert m["sortino"] > m["sharpe"] > 0


def test_monthly_returns_uses_month_end_over_prev_month_end():
    monthly = _monthly_returns([100, 110, 120, 130],
                               ["2026-01-05", "2026-01-20", "2026-02-03", "2026-02-28"])
    # 首月无「上月末」基准，不虚构；2 月 = 130/110-1
    assert monthly == {"2026-02": round(130 / 110 - 1, 6)}
    m = compute_metrics([100, 110, 120, 130], period="1d",
                        dates=["2026-01-05", "2026-01-20", "2026-02-03", "2026-02-28"])
    assert m["monthly_win_rate"] == 1.0
    assert m["best_month"] == ["2026-02", round(130 / 110 - 1, 6)]


def test_month_key_variants():
    assert _month_key("2026-08-30") == "2026-08"
    assert _month_key("2026-08-30 15:00:00") == "2026-08"
    assert _month_key("20260830") == "2026-08"
    assert _month_key("2026/08/30") == "2026-08"
    assert _month_key(None) is None
    assert _month_key("") is None


def test_backward_compatible_keys_still_present():
    m = compute_metrics(_EQUITY, _TRADES, period="1d")
    for k in ("total_return", "annual_return", "annual_volatility", "sharpe",
              "max_drawdown", "calmar", "win_rate", "trade_count", "avg_pnl",
              "var95", "rating", "annualization", "period"):
        assert k in m, k


def test_minute_bar_annualization_factor():
    a = compute_metrics(_EQUITY, period="1d")
    b = compute_metrics(_EQUITY, period="5m")
    assert a["annualization"] == 252
    assert b["annualization"] == 252 * 48
