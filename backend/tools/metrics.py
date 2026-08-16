"""严谨绩效指标（零 mock，纯计算）。

相对旧 `_metrics` 的增强：
- 年化按 bar 频率换算（日线 252；分钟线按日内 bar 数），不再硬编码 252
- 夏普 rf 可配置（默认 0）
- 新增：基准收益、alpha、beta、信息比率(IR)、Calmar
- 全部为纯函数，便于单测与回测/模拟盘/实盘复用
"""
from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence


# A 股典型日内 bar 数（用于分钟线年化换算；可按实际交易时段覆盖）
_BARS_PER_DAY = {
    "1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4,
}


def bars_per_day(period: str = "1d") -> int:
    """单交易日内的 bar 数（分钟线）。

    日线/周线/月线返回 1（年化用交易日数 252 而非 bar 数）。
    """
    return _BARS_PER_DAY.get(period, 1)


def annualization_factor(period: str = "1d") -> int:
    """年化乘子（用于波动率/夏普/收益的年化）。

    日线：252 个交易日；分钟线：252 × 日内 bar 数；周线：52；月线：12。
    """
    p = (period or "1d").lower()
    if p in ("1d", "day", "daily"):
        return 252
    if p in ("1w", "week", "weekly"):
        return 52
    if p in ("1mon", "1m_month", "month", "monthly"):
        return 12
    if p in _BARS_PER_DAY:
        return 252 * _BARS_PER_DAY[p]
    return 252


def _daily_returns_to_period(returns: Sequence[float], ann: int) -> float:
    """把周期收益序列年化（几何）。"""
    if not returns:
        return 0.0
    total = 1.0
    for r in returns:
        total *= (1.0 + r)
    n = len(returns)
    if total <= 0:
        return -1.0
    return (total ** (ann / n)) - 1.0


def compute_metrics(equity: Sequence[float],
                    trades: Optional[Sequence[dict]] = None,
                    period: str = "1d",
                    benchmark_rets: Optional[Sequence[float]] = None,
                    rf: float = 0.0) -> dict:
    """计算一套严谨绩效指标。

    参数：
      equity:          净值序列（首个为初始资金）
      trades:          成交明细（需含 side/pnl），可选
      period:          bar 频率（"1d"/"5m"/...），决定年化乘子
      benchmark_rets:  基准逐周期收益（与 equity 等长-1），用于 alpha/beta/IR
      rf:              无风险年化收益率（默认 0）

    返回字典：总收益/年化/最大回撤/年化波动/夏普/胜率/交易数/平均盈亏/
              VaR95/alpha/beta/信息比率/Calmar/评级。
    """
    equity = list(equity)
    if len(equity) < 2:
        return {}
    ann = annualization_factor(period)
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    n = len(rets)
    total = equity[-1] / equity[0] - 1
    annual_return = _daily_returns_to_period(rets, ann) if n else 0.0
    mean = statistics.mean(rets) if rets else 0.0
    std = statistics.stdev(rets) if len(rets) > 1 else 0.0
    rf_period = rf / ann if ann else 0.0
    excess = (mean - rf_period) if std > 0 else 0.0
    sharpe = (excess / std * math.sqrt(ann)) if std > 0 else 0.0

    # 最大回撤
    peak = equity[0]
    max_dd = 0.0
    for c in equity:
        peak = max(peak, c)
        max_dd = min(max_dd, c / peak - 1)

    # 交易统计
    trades = list(trades or [])
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_pnl = statistics.mean([t.get("pnl", 0) for t in trades]) if trades else 0.0
    var95 = -sorted(rets)[int(n * 0.05)] if n >= 20 else 0.0
    ann_vol = std * math.sqrt(ann) if std > 0 else 0.0

    # 基准相关（alpha / beta / IR）
    alpha = beta = information_ratio = None
    bench_annual = None
    if benchmark_rets and len(benchmark_rets) == n and n > 1:
        b = list(benchmark_rets)
        b_mean = statistics.mean(b)
        # beta = Cov(r,b)/Var(b)，分子分母同归一化（同除 n 或同除 n-1 抵消），避免混用口径
        num = sum((rets[i] - mean) * (b[i] - b_mean) for i in range(n))
        den = sum((b[i] - b_mean) ** 2 for i in range(n))
        beta = num / den if den > 0 else 0.0
        bench_annual = _daily_returns_to_period(b, ann)
        # CAPM alpha 用「算术均值年化」口径（与 beta/IR 一致），避免与几何年化混用
        port_excess_arith = (mean - rf_period) * ann
        bench_excess_arith = (b_mean - rf_period) * ann
        alpha = port_excess_arith - beta * bench_excess_arith
        # 信息比率：主动收益(mean - b_mean) / 跟踪误差
        active = [rets[i] - b[i] for i in range(n)]
        te = statistics.stdev(active) if len(active) > 1 else 0.0
        information_ratio = (statistics.mean(active) / te * math.sqrt(ann)) if te > 0 else 0.0

    calmar = (annual_return / abs(max_dd)) if max_dd < 0 else (annual_return if annual_return > 0 else 0.0)

    out = {
        "total_return": round(total, 4),
        "annual_return": round(annual_return, 4),
        "annual_volatility": round(ann_vol, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "calmar": round(calmar, 3) if calmar is not None else None,
        "win_rate": round(win_rate, 3),
        "trade_count": len(trades),
        "avg_pnl": round(avg_pnl, 4),
        "var95": round(var95, 4),
        "rating": "A" if sharpe > 1.5 else ("B" if sharpe > 1 else "C"),
        # 诚信标注：指标基于何种 bar 频率年化
        "annualization": ann,
        "period": period,
    }
    if beta is not None:
        out["beta"] = round(beta, 3)
        out["alpha"] = round(alpha, 4)
        out["bench_annual_return"] = round(bench_annual, 4)
    if information_ratio is not None:
        out["information_ratio"] = round(information_ratio, 3)
    if rf:
        out["rf"] = rf
    return out
