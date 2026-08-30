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


def _month_key(d) -> Optional[str]:
    """把日期归一化为 "YYYY-MM"。

    兼容 "2026-08-30"/"2026-08-30 15:00:00"/"20260830"/"2026/08/30" 等写法；
    无法解析返回 None（跳过该 bar，不猜测）。
    """
    if d is None:
        return None
    s = str(d).strip()
    if len(s) >= 7 and s[4] in "-/.":
        return f"{s[:4]}-{s[5:7]}"
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}"
    if len(s) >= 6 and s[:6].isdigit():
        return f"{s[:4]}-{s[4:6]}"
    return None


def _monthly_returns(equity: Sequence[float], dates: Sequence) -> dict:
    """按月聚合收益（月收益 = 月末净值 / 上月末净值 - 1）。

    第一个月缺少「上月末」基准，故不计入（不虚构收益）。
    """
    if not dates or len(dates) != len(equity) or len(equity) < 2:
        return {}
    monthly: dict[str, float] = {}
    cur_key = None
    prev_close = None      # 上月末净值
    last_close = None      # 当前月最新净值
    for i, d in enumerate(dates):
        key = _month_key(d)
        if key is None:
            continue
        if key != cur_key:
            if cur_key and prev_close and last_close is not None:
                monthly[cur_key] = round(last_close / prev_close - 1, 6)
            if cur_key is not None:
                prev_close = last_close
            cur_key = key
            last_close = None
        last_close = equity[i]
    if cur_key and prev_close and last_close is not None:
        monthly[cur_key] = round(last_close / prev_close - 1, 6)
    return monthly


def compute_metrics(equity: Sequence[float],
                    trades: Optional[Sequence[dict]] = None,
                    period: str = "1d",
                    benchmark_rets: Optional[Sequence[float]] = None,
                    rf: float = 0.0,
                    dates: Optional[Sequence] = None) -> dict:
    """计算一套严谨绩效指标。

    参数：
      equity:          净值序列（每个 bar 一个点位）
      trades:          成交明细（需含 side/pnl），可选
      period:          bar 频率（"1d"/"5m"/...），决定年化乘子
      benchmark_rets:  基准逐周期收益（与 equity 等长-1），用于 alpha/beta/IR
      rf:              无风险年化收益率（默认 0）
      dates:           与 equity 等长的日期序列，可选。提供后额外输出
                       最大回撤的发生/回复区间与月度收益分布

    返回字典：总收益/年化/年化波动/夏普/Sortino/最大回撤(含区间)/Calmar/
              胜率/交易数/平均盈亏/盈亏比/利润因子/VaR95/CVaR95/
              alpha/beta/信息比率/评级。
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

    # 最大回撤（含发生区间：峰值 → 谷底 → 回复，便于定位风险究竟发生在哪一段）
    peak = equity[0]
    peak_i = 0
    max_dd = 0.0
    dd_peak_i = 0
    dd_trough_i = 0
    for i, c in enumerate(equity):
        if c > peak:
            peak = c
            peak_i = i
        dd = (c / peak - 1) if peak else 0.0
        if dd < max_dd:
            max_dd = dd
            dd_peak_i = peak_i
            dd_trough_i = i
    # 回复：自谷底往后找第一个回到峰值水位的 bar；序列末尾仍未回复则为 None
    dd_recovery_i = None
    if max_dd < 0:
        peak_level = equity[dd_peak_i]
        for j in range(dd_trough_i, len(equity)):
            if equity[j] >= peak_level:
                dd_recovery_i = j
                break

    # Sortino：只惩罚下行波动（上行波动是收益，不该计入风险）。
    # 下行偏差用标准定义 sqrt(mean(min(0, r-target)^2))，而非 stdev（后者自由度口径不同）。
    downside = [min(0.0, r - rf_period) for r in rets]
    dd_var = (sum(d * d for d in downside) / n) if n else 0.0
    dd_std = math.sqrt(dd_var)
    sortino = (excess / dd_std * math.sqrt(ann)) if dd_std > 0 else 0.0

    # 交易统计
    trades = list(trades or [])
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_pnl = statistics.mean([t.get("pnl", 0) for t in trades]) if trades else 0.0
    # VaR/CVaR 需要足够样本才有统计意义。样本不足时返回 None 而不是 0：
    # 0 会被误读成「没有尾部风险」，这是最危险的假数据。
    tail_ok = n >= 20
    var95 = -sorted(rets)[int(n * 0.05)] if tail_ok else None
    # CVaR95：最差 5% 收益的条件均值（尾部平均损失），比 VaR 更能刻画极端风险
    if tail_ok:
        tail_n = max(1, int(n * 0.05))
        cvar95 = -statistics.mean(sorted(rets)[:tail_n])
    else:
        cvar95 = None
    ann_vol = std * math.sqrt(ann) if std > 0 else 0.0

    # 盈亏结构：利润因子(总盈/总亏) 与 盈亏比(平均盈/平均亏)，无亏损交易时为 None
    gross_profit = sum(t.get("pnl", 0) for t in wins)
    gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
    avg_win = statistics.mean([t.get("pnl", 0) for t in wins]) if wins else 0.0
    avg_loss = abs(statistics.mean([t.get("pnl", 0) for t in losses])) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else None

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
        "sortino": round(sortino, 3),
        "max_drawdown": round(max_dd, 4),
        "calmar": round(calmar, 3) if calmar is not None else None,
        "win_rate": round(win_rate, 3),
        "trade_count": len(trades),
        "avg_pnl": round(avg_pnl, 4),
        "var95": round(var95, 4) if var95 is not None else None,
        "cvar95": round(cvar95, 4) if cvar95 is not None else None,
        "rating": "A" if sharpe > 1.5 else ("B" if sharpe > 1 else "C"),
        # 诚信标注：指标基于何种 bar 频率年化
        "annualization": ann,
        "period": period,
    }
    if not tail_ok:
        out["tail_metrics_note"] = f"样本不足（{n} 个收益观测 < 20），VaR/CVaR 不具统计意义，未输出"
    # 交易结构（无成交或无亏损交易时给出 None，而不是伪造 0）
    if trades:
        out["profit_factor"] = round(profit_factor, 3) if profit_factor is not None else None
        out["payoff_ratio"] = round(payoff_ratio, 3) if payoff_ratio is not None else None
        out["avg_win"] = round(avg_win, 4)
        out["avg_loss"] = round(avg_loss, 4)
    # 最大回撤的「发生区间 / 回复情况」：只知道数值无法定位风险发生在哪一段
    if max_dd < 0:
        dts = list(dates) if dates else None
        out["max_drawdown_bars"] = max(0, dd_trough_i - dd_peak_i)
        if dts and len(dts) == len(equity):
            out["max_drawdown_start"] = dts[dd_peak_i]
            out["max_drawdown_end"] = dts[dd_trough_i]
            out["max_drawdown_recovery"] = dts[dd_recovery_i] if dd_recovery_i is not None else None
        else:
            out["max_drawdown_start_idx"] = dd_peak_i
            out["max_drawdown_end_idx"] = dd_trough_i
            out["max_drawdown_recovery_idx"] = dd_recovery_i
        out["max_drawdown_recovered"] = dd_recovery_i is not None
    # 月度收益分布（需要日期序列；缺失则不输出，不做任何假设）
    monthly = _monthly_returns(equity, dates) if dates else {}
    if monthly:
        vals = list(monthly.values())
        out["monthly_returns"] = monthly
        out["monthly_win_rate"] = round(sum(1 for v in vals if v > 0) / len(vals), 3)
        best_m = max(monthly.items(), key=lambda kv: kv[1])
        worst_m = min(monthly.items(), key=lambda kv: kv[1])
        out["best_month"] = [best_m[0], best_m[1]]
        out["worst_month"] = [worst_m[0], worst_m[1]]
    if beta is not None:
        out["beta"] = round(beta, 3)
        out["alpha"] = round(alpha, 4)
        out["bench_annual_return"] = round(bench_annual, 4)
    if information_ratio is not None:
        out["information_ratio"] = round(information_ratio, 3)
    if rf:
        out["rf"] = rf
    return out
