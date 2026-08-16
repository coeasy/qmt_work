"""研究深度层（阶段 3）：因子分析 + 组合回测 + walk-forward + 绩效归因。

全部为纯计算（零 mock、不造假数据），输入来自真实 K 线 / 真实账户成交：
- 因子分析：IC / ICIR（滚动截面相关）、分位（分位数）分组、因子相关性矩阵
- 组合回测：N 标的 × 权重矩阵，复用统一撮合内核；输出可喂回 `target_portfolio` 的目标持仓
- walk-forward：滚动窗口 train/test + 参数自动寻优（复用 run_param_sweep），输出稳健性报告
- 绩效归因：分标的 / 分策略（买卖侧）/ 滑点 / 成本拆解

MCP 与 REST 仅做「取真实 K 线 → 调用纯函数」的薄封装；降级路径显式标注来源。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import fetch_kline_cached
from .backtest import _build_cfg, _signals_vectorized, run_param_sweep
from .factors import compute_factor, from_kline, _EXTRA_FIELDS
from .matching import simulate as match_simulate
from .metrics import compute_metrics
from xtquant_client.base import BrokerNotConnectedError


# ============================================================================
# 基础统计（纯 Python，确定性，可单测）
# ============================================================================

def _clean_pairs(a: Sequence[Optional[float]], b: Sequence[Optional[float]]
                 ) -> Tuple[List[float], List[float]]:
    """成对剔除 None / NaN，返回对齐的 (xs, ys)。"""
    xs, ys = [], []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        if isinstance(x, float) and math.isnan(x):
            continue
        if isinstance(y, float) and math.isnan(y):
            continue
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    xs, ys = _clean_pairs(xs, ys)
    n = len(xs)
    if n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank_avg(xs: List[float]) -> List[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    xs, ys = _clean_pairs(xs, ys)
    if len(xs) < 3:
        return None
    rx, ry = _rank_avg(xs), _rank_avg(ys)
    return _pearson(rx, ry)


# ============================================================================
# 1. 因子 IC / ICIR
# ============================================================================

def factor_ic(factor: Sequence[float], forward_return: Sequence[float],
              method: str = "pearson") -> Optional[float]:
    """单序列因子 IC：因子值 t 与远期收益 t 的（秩）相关。

    用于「单标的、时间序列」场景：IC 为全样本标量相关；
    若需要 ICIR（多期 IC 的均值/波动），请用 `factor_ic_panel` 产生逐期 IC 序列。
    method: "pearson" | "spearman"。
    """
    if method == "spearman":
        return _spearman(list(factor), list(forward_return))
    return _pearson(list(factor), list(forward_return))


def factor_ic_panel(factor_panel: Sequence[Sequence[float]],
                    return_panel: Sequence[Sequence[float]],
                    method: str = "pearson") -> List[Optional[float]]:
    """截面逐期 IC：每个时间截面 t 上，对全市场因子值与远期收益做（秩）相关。

    factor_panel / return_panel：等长的时间序列，每项是该时刻的截面（各标的）列表。
    返回与输入等长的逐期 IC 列表（截面退化时该期为 None）。
    """
    T = min(len(factor_panel), len(return_panel))
    out: List[Optional[float]] = []
    for t in range(T):
        fa = list(factor_panel[t])
        ra = list(return_panel[t])
        if method == "spearman":
            out.append(_spearman(fa, ra))
        else:
            out.append(_pearson(fa, ra))
    return out


def ic_statistics(ic_list: Sequence[Optional[float]]) -> dict:
    """对逐期 IC 序列汇总：均值 / 波动 / ICIR / 胜率 / t 值 / 显著性。

    ICIR = mean(IC) / std(IC)；positive_ratio = IC>0 占比；
    t_stat = mean / (std/√n)；|t_stat|>1.96 视为显著。
    """
    vals = [x for x in ic_list if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not vals:
        return {"n": 0, "ic_mean": None, "ic_std": None, "icir": None,
                "positive_ratio": None, "t_stat": None, "significant": False}
    m = _mean(vals)
    sd = _stdev(vals)
    n = len(vals)
    icir = (m / sd) if sd > 0 else None
    pos = sum(1 for v in vals if v > 0) / n
    t_stat = (m / (sd / math.sqrt(n))) if sd > 0 else None
    return {
        "n": n,
        "ic_mean": round(m, 4),
        "ic_std": round(sd, 4),
        "icir": round(icir, 3) if icir is not None else None,
        "positive_ratio": round(pos, 3),
        "t_stat": round(t_stat, 3) if t_stat is not None else None,
        "significant": bool(t_stat is not None and abs(t_stat) > 1.96),
    }


# ============================================================================
# 2. 分位（分位数）分组
# ============================================================================

def quantile_analysis(factor: Sequence[float], forward_return: Sequence[float],
                      n_q: int = 5) -> dict:
    """分位分组：按因子值分箱，统计各分位远期收益均值 / 累积，及多空（top-bottom）价差。

    接受「配对样本」(因子值, 远期收益)（单标的时序或多标的截面混合均可）。
    - quantiles: 各分位 min/max/avg_return/count/cum_return
    - long_short_avg_return: 最高分位均值 - 最低分位均值（因子单调性代理）
    返回结构可直接绘图（spread_by_quantile）。
    """
    xs, ys = _clean_pairs(list(factor), list(forward_return))
    n = len(xs)
    if n < n_q * 2:
        raise ValueError(f"样本不足：需 ≥ {n_q * 2} 个配对样本，当前 {n}")
    # 分位边界（等计数组）
    order = sorted(range(n), key=lambda i: xs[i])
    bins: List[List[int]] = [[] for _ in range(n_q)]
    for rank, idx in enumerate(order):
        q = min(n_q - 1, rank * n_q // n)
        bins[q].append(idx)
    quants = []
    spread: List[float] = []
    for q in range(n_q):
        idxs = bins[q]
        rets = [ys[i] for i in idxs]
        avg = _mean(rets)
        cum = 1.0
        for r in rets:
            cum *= (1.0 + r)
        fvals = [xs[i] for i in idxs]
        quants.append({
            "q": q + 1,
            "min": round(min(fvals), 6),
            "max": round(max(fvals), 6),
            "avg_return": round(avg, 6),
            "count": len(idxs),
            "cum_return": round(cum - 1.0, 6),
        })
        spread.append(avg)
    top = quants[-1]["avg_return"]
    bottom = quants[0]["avg_return"]
    # 多空组合逐期收益 = top 组样本收益 - bottom 组样本收益
    ls_samples = [ys[i] for i in bins[-1]] + [-ys[i] for i in bins[0]]
    ls_mean = _mean(ls_samples)
    ls_sd = _stdev(ls_samples)
    return {
        "n_quantiles": n_q,
        "n_samples": n,
        "quantiles": quants,
        "spread_by_quantile": [round(s, 6) for s in spread],
        "long_short_avg_return": round(top - bottom, 6),
        "long_short_sharpe": round(ls_mean / ls_sd * math.sqrt(252), 3) if ls_sd > 0 else None,
    }


# ============================================================================
# 3. 因子相关性矩阵
# ============================================================================

def factor_correlation(factor_dict: Dict[str, List[float]],
                       method: str = "pearson") -> dict:
    """因子相关性矩阵：对一组等长因子序列两两计算（秩）相关。

    factor_dict: {因子名: 序列}；以最短长度对齐。返回 names + matrix。
    """
    names = list(factor_dict.keys())
    seqs = {k: list(v) for k, v in factor_dict.items()}
    L = min(len(v) for v in seqs.values()) if seqs else 0
    if L < 3:
        raise ValueError("因子序列长度不足（需 ≥ 3）")
    aligned = {k: v[:L] for k, v in seqs.items()}
    matrix = {}
    for a in names:
        row = {}
        for b in names:
            if a == b:
                row[b] = 1.0
            else:
                c = _spearman(aligned[a], aligned[b]) if method == "spearman" else _pearson(aligned[a], aligned[b])
                row[b] = round(c, 4) if c is not None else None
        matrix[a] = row
    return {"method": method, "names": names, "matrix": matrix}


# ============================================================================
# 4. 多标的组合回测（N 标的 × 权重矩阵）
# ============================================================================

def _normalize_weights(weights: Optional[List[float]], n: int) -> List[float]:
    if not weights:
        return [1.0 / n] * n
    if len(weights) != n:
        raise ValueError(f"权重数量({len(weights)})与标的数量({n})不一致")
    s = sum(weights)
    if s <= 0:
        return [1.0 / n] * n
    return [w / s for w in weights]


def run_portfolio_backtest(symbols: List[str], klines: Dict[str, list],
                          weights: Optional[List[float]], strategy: str,
                          params: dict, initial_capital: float,
                          commission_rate: float = 0.0003,
                          stamp_tax: float = 0.001, slippage_bps: float = 5.0,
                          *, execution_timing: str = "close",
                          enforce_limit: bool = True,
                          max_participation_pct: float = 1.0,
                          use_spread_slippage: bool = False,
                          period: str = "1d", is_st: bool = False,
                          rf: float = 0.0) -> dict:
    """多标的组合回测：每个标的独立用统一撮合内核回测，按权重汇总净值。

    - 每个标的分配 initial_capital * w_i，撮合内核满仓切换；
    - 组合净值 = Σ 各标的(净值 × w_i)；指标由组合净值统一计算；
    - 输出各标的指标（用于归因）+ 末态目标持仓（闭环到 target_portfolio_sync）。
    source 始终为真实 K 线。
    """
    n = len(symbols)
    if n == 0:
        raise ValueError("至少需 1 个标的")
    w = _normalize_weights(weights, n)

    per_symbol_equity: Dict[str, list] = {}
    per_symbol_metrics: Dict[str, dict] = {}
    per_symbol_signal: Dict[str, list] = {}
    last_prices: Dict[str, float] = {}
    for i, sym in enumerate(symbols):
        kline = klines.get(sym) or []
        closes = [b["close"] for b in kline if b.get("close") is not None]
        if len(closes) < 30:
            raise BrokerNotConnectedError(f"{sym} K 线不足（需≥30 根），请确认券商已返回历史数据。")
        sig = _signals_vectorized(strategy, closes, params)
        cfg = _build_cfg(sym, commission_rate, stamp_tax, slippage_bps,
                         execution_timing=execution_timing, enforce_limit=enforce_limit,
                         max_participation_pct=max_participation_pct,
                         use_spread_slippage=use_spread_slippage, is_st=is_st)
        sim = match_simulate(closes, sig, kline, cfg, initial_capital)
        scaled = [e * w[i] for e in sim["equity"]]
        per_symbol_equity[sym] = scaled
        per_symbol_metrics[sym] = compute_metrics(scaled, sim["trades"], period=period, rf=rf)
        per_symbol_signal[sym] = sig
        last_prices[sym] = closes[-1]

    # 组合净值
    T = max(len(v) for v in per_symbol_equity.values())
    portfolio_equity: List[float] = []
    for t in range(T):
        tot = 0.0
        for sym in symbols:
            eq = per_symbol_equity[sym]
            tot += eq[t] if t < len(eq) else eq[-1]
        portfolio_equity.append(tot)
    portfolio_metrics = compute_metrics(portfolio_equity, None, period=period, rf=rf)

    # 末态目标持仓（回测→实盘同一份目标持仓闭环）
    equity_final = portfolio_equity[-1]
    target_portfolio: Dict[str, int] = {}
    target_weights: Dict[str, float] = {}
    for i, sym in enumerate(symbols):
        held = bool(per_symbol_signal[sym][-1]) if per_symbol_signal[sym] else False
        if held and last_prices[sym] > 0:
            mv = w[i] * equity_final
            vol = int(mv / last_prices[sym] / 100) * 100
            target_portfolio[sym] = max(0, vol)
            target_weights[sym] = w[i]
        else:
            target_portfolio[sym] = 0
            target_weights[sym] = 0.0

    return {
        "symbols": symbols,
        "weights": [round(x, 4) for x in w],
        "strategy": strategy, "params": params,
        "initial_capital": initial_capital,
        "portfolio_metrics": portfolio_metrics,
        "portfolio_equity_curve": [round(e, 2) for e in portfolio_equity[-200:]],
        "per_symbol_metrics": per_symbol_metrics,
        "target_portfolio": target_portfolio,
        "target_weights": target_weights,
        "source": "real_kline",
        "note": "target_portfolio 可直接喂入 target_portfolio_sync（volume 模式）实现回测→实盘同一份目标持仓",
    }


# ============================================================================
# 5. walk-forward（滚动窗口 train/test + 参数寻优）
# ============================================================================

def walk_forward(symbol: str, kline: list, strategy: str, params: dict,
                 initial_capital: float,
                 commission_rate: float = 0.0003, stamp_tax: float = 0.001,
                 slippage_bps: float = 5.0, *,
                 window: int = 120, step: int = 60,
                 execution_timing: str = "close",
                 enforce_limit: bool = True,
                 max_participation_pct: float = 1.0,
                 use_spread_slippage: bool = False,
                 period: str = "1d", is_st: bool = False, rf: float = 0.0,
                 optimize: bool = False, param_grid: Optional[dict] = None) -> dict:
    """滚动窗口 walk-forward：每个 fold 用 train 窗口（可选参数寻优）定参，在 test 窗口做样本外验证。

    - window：train 长度；step：test 长度（相邻 fold 间隔）。
    - optimize=True 时对每个 train 窗口跑 run_param_sweep 取最优参数（按夏普）。
    - 返回各 fold 的样本外指标 + 稳健性汇总（夏普分布 / 参数漂移）。
    """
    closes = [b["close"] for b in kline if b.get("close") is not None]
    n = len(closes)
    if n < window + step + 10:
        raise BrokerNotConnectedError(f"{symbol} K 线不足（需≥{window + step + 10} 根）支撑 walk-forward。")

    folds: List[dict] = []
    s = 0
    while s + window + step <= n:
        e = s + window
        e2 = e + step
        train_kline = kline[s:e]
        test_kline = kline[e:e2]
        test_closes = closes[e:e2]
        if optimize and param_grid:
            try:
                sweep = run_param_sweep(symbol, train_kline, strategy, param_grid,
                                        initial_capital, commission_rate, stamp_tax, slippage_bps)
                fold_params = (sweep.get("best") or {}).get("params") or dict(params)
            except (BrokerNotConnectedError, ValueError):
                fold_params = dict(params)
        else:
            fold_params = dict(params)
        sig = _signals_vectorized(strategy, test_closes, fold_params)
        cfg = _build_cfg(symbol, commission_rate, stamp_tax, slippage_bps,
                         execution_timing=execution_timing, enforce_limit=enforce_limit,
                         max_participation_pct=max_participation_pct,
                         use_spread_slippage=use_spread_slippage, is_st=is_st)
        sim = match_simulate(test_closes, sig, test_kline, cfg, initial_capital)
        m = compute_metrics(sim["equity"], sim["trades"], period=period, rf=rf)
        folds.append({
            "train_range": [s, e], "test_range": [e, e2],
            "params": fold_params, "metrics": m,
        })
        s += step

    if not folds:
        raise ValueError("未生成任何 fold，请调小 window/step 或提供更多 K 线。")

    test_sharpes = [f["metrics"].get("sharpe", 0.0) for f in folds]
    test_returns = [f["metrics"].get("total_return", 0.0) for f in folds]
    sd = _stdev(test_sharpes)
    mean_sh = _mean(test_sharpes)
    robustness = "stable" if (mean_sh > 0 and sd < abs(mean_sh)) else ("positive" if mean_sh > 0 else "unstable")

    summary = {
        "n_folds": len(folds),
        "window": window, "step": step,
        "test_sharpe_mean": round(mean_sh, 3),
        "test_sharpe_std": round(sd, 3),
        "test_sharpe_min": round(min(test_sharpes), 3),
        "test_sharpe_max": round(max(test_sharpes), 3),
        "test_return_mean": round(_mean(test_returns), 4),
        "positive_folds": sum(1 for x in test_sharpes if x > 0),
        "robustness": robustness,
    }
    if optimize and param_grid:
        drift: Dict[str, list] = {}
        for f in folds:
            for k, v in f["params"].items():
                drift.setdefault(k, []).append(v)
        summary["param_drift"] = drift

    return {"symbol": symbol, "strategy": strategy,
            "optimize": bool(optimize and param_grid),
            "folds": folds, "summary": summary, "source": "real_kline"}


# ============================================================================
# 6. 绩效归因增强（分标的 / 分策略 / 滑点 / 成本拆解）
# ============================================================================

def attribute_pnl(trades: List[dict], klines_by_symbol: Dict[str, list]) -> dict:
    """绩效归因：分标的贡献 + 分买卖侧 + 滑点（成交价 vs 次根开盘/VWAP）+ 成本拆解。

    纯计算：trades 含 {code, side, price, qty, time?}；klines_by_symbol 用于取次根参考价。
    - by_symbol：各标的已实现盈亏（卖出 pnl 累加）
    - by_side：买入成本（负）vs 卖出收入（正）
    - slippage：逐笔成交价 vs 次根开盘/次根 VWAP 的基点差（仅在有次根数据时）
    - cost：佣金 + 印花税 + 滑点成本 总拆解
    """
    by_symbol: Dict[str, float] = {}
    by_side: Dict[str, float] = {"buy": 0.0, "sell": 0.0}
    total_commission = 0.0
    total_stamp = 0.0
    slip_samples: List[dict] = []

    # 建立 code -> {time: bar} 索引（按日前缀定位次根）
    idx: Dict[str, Dict[str, dict]] = {}
    for code, kline in klines_by_symbol.items():
        idx[code] = {str(b.get("time", ""))[:10]: b for b in kline}

    for t in trades:
        code = t.get("code") or t.get("symbol") or "?"
        side = t.get("side")
        price = float(t.get("price") or 0)
        qty = float(t.get("qty") or t.get("volume") or 0)
        pnl = float(t.get("pnl") or 0)
        # 分标的贡献：以已实现 pnl 计（未实现不计入）
        if side == "sell":
            by_symbol[code] = by_symbol.get(code, 0.0) + pnl
        by_side[side] = by_side.get(side, 0.0) + (price * qty if side == "sell" else -price * qty)

        # 成本拆解（按标准费率重建，仅作量级参考）
        commission = price * qty * 0.0003
        stamp = price * qty * 0.001 if side == "sell" else 0.0
        total_commission += commission
        total_stamp += stamp

        # 滑点：成交价 vs 次根开盘 / 次根 VWAP
        day = str(t.get("time", ""))[:10]
        kl = idx.get(code, {})
        days = sorted(kl.keys())
        nxt = None
        for d in days:
            if d > day:
                nxt = kl[d]
                break
        if nxt is not None and nxt.get("open"):
            open_p = float(nxt["open"])
            vwap = (float(nxt.get("amount") or 0) / float(nxt["volume"])) if nxt.get("volume") else None
            ref = vwap if vwap else open_p
            if ref > 0:
                # 买入：成交价高于参考价 = 不利（正 bps）；卖出相反
                sign = 1.0 if side == "buy" else -1.0
                bps_val = sign * (price - ref) / ref * 1e4
                slip_samples.append({
                    "code": code, "side": side, "price": price,
                    "ref_open": round(open_p, 4),
                    "ref_vwap": round(vwap, 4) if vwap else None,
                    "slippage_bps": round(bps_val, 2),
                })

    slip_vals = [s["slippage_bps"] for s in slip_samples]
    avg_slip = _mean(slip_vals) if slip_vals else None

    return {
        "by_symbol": {c: round(v, 2) for c, v in by_symbol.items()},
        "by_side": {k: round(v, 2) for k, v in by_side.items()},
        "cost": {
            "commission_est": round(total_commission, 2),
            "stamp_tax_est": round(total_stamp, 2),
            "total_est": round(total_commission + total_stamp, 2),
        },
        "slippage": {
            "samples": slip_samples,
            "avg_slippage_bps": round(avg_slip, 2) if avg_slip is not None else None,
            "n": len(slip_samples),
        },
        "total_pnl": round(sum(by_symbol.values()), 2),
    }


# ============================================================================
# 异步薄封装：取真实 K 线 → 调用纯函数
# ============================================================================

async def _factor_and_forward(symbol: str, factor_name: str, period: str,
                              count: int, broker_id: Optional[str],
                              forward: int = 1) -> Tuple[list, list, str]:
    """计算单标的因子序列 + 次期远期收益序列（配对用），返回 (factor, fwd_ret, source)。"""
    res = await fetch_kline_cached(symbol, period, count, broker_id=broker_id or None)
    bars = res.get("bars") or []
    if len(bars) < 30:
        raise BrokerNotConnectedError(f"{symbol} K 线不足（需≥30 根）")
    close = from_kline(bars, "close")
    shared = {"close": close}
    for fld in ("high", "low", "volume"):
        shared[fld] = from_kline(bars, fld)
    # 仅注入该因子真正需要的额外序列（避免把 high/low/volume 误传给不需要的因子）
    fp = {fld: shared[fld] for fld in _EXTRA_FIELDS.get(factor_name, [])}
    fvals = compute_factor(factor_name, close, **fp)
    # 远期收益：ret[t] = close[t+forward]/close[t] - 1
    fwd: List[Optional[float]] = []
    L = len(close)
    for t in range(L):
        if t + forward < L and close[t] and close[t] > 0:
            fwd.append(close[t + forward] / close[t] - 1.0)
        else:
            fwd.append(None)
    return fvals, fwd, res.get("source", "unknown")


def register_research_tools(mcp):
    @mcp.tool()
    async def factor_ic_analysis(
        symbol: str, factor_name: str = "rsi", period: str = "1d",
        count: int = 250, broker_id: str = "", forward: int = 1,
        mode: str = "series", method: str = "pearson",
    ) -> dict:
        """因子 IC / ICIR 分析（阶段 3 研究层）。

        mode="series"：单标的，返回全样本因子 IC（因子值 vs 次期收益相关）；
        mode="panel"：传 symbols(逗号分隔) 做截面逐期 IC → ICIR（均值/波动/胜率/t值）。
        所有序列来自真实券商 K 线，无假数据；source 标注数据来源。
        """
        if mode == "panel":
            symbols = [s.strip().upper() for s in symbol.split(",") if s.strip()]
            if not symbols:
                return {"ok": False, "reason": "panel 模式需 symbols（逗号分隔）"}
            panels_f, panels_r = [], []
            src = None
            for sym in symbols:
                fv, fwd, s = await _factor_and_forward(sym, factor_name, period, count,
                                                       broker_id, forward)
                panels_f.append(fv); panels_r.append(fwd); src = s
            ic_list = factor_ic_panel(panels_f, panels_r, method=method)
            return {"symbols": symbols, "factor_name": factor_name, "mode": "panel",
                    "method": method, "forward": forward, "source": src,
                    "ic_series": [None if v is None else round(v, 4) for v in ic_list],
                    "stats": ic_statistics(ic_list)}
        fv, fwd, src = await _factor_and_forward(symbol, factor_name, period, count,
                                                 broker_id, forward)
        ic = factor_ic(fv, fwd, method=method)
        return {"symbol": symbol, "factor_name": factor_name, "mode": "series",
                "method": method, "forward": forward, "source": src,
                "ic": round(ic, 4) if ic is not None else None,
                "note": "单序列 IC 为全样本相关；ICIR 需多期 IC（用 mode=panel 或多标的截面）。"}

    @mcp.tool()
    async def factor_quantile_analysis(
        symbol: str, factor_name: str = "rsi", period: str = "1d",
        count: int = 250, broker_id: str = "", forward: int = 1,
        n_q: int = 5,
    ) -> dict:
        """分位（分位数）分组分析：按因子值分箱，统计各分位远期收益均值与多空价差。"""
        fv, fwd, src = await _factor_and_forward(symbol, factor_name, period, count,
                                                 broker_id, forward)
        out = quantile_analysis(fv, fwd, n_q=n_q)
        out["symbol"] = symbol; out["factor_name"] = factor_name
        out["forward"] = forward; out["source"] = src
        return out

    @mcp.tool()
    async def factor_correlation_matrix(
        symbols: str, factor_name: str = "rsi", period: str = "1d",
        count: int = 250, broker_id: str = "", method: str = "pearson",
    ) -> dict:
        """因子相关性矩阵：对多个标的同一因子（或单标的多因子）做两两（秩）相关。

        symbols：逗号分隔的标的列表（跨标的同因子相关性）；要跨因子则改用 REST 接口传入 factor_dict。
        """
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if len(syms) < 2:
            return {"ok": False, "reason": "至少需 2 个标的/因子"}
        fd: Dict[str, list] = {}
        for sym in syms:
            fv, _fwd, _s = await _factor_and_forward(sym, factor_name, period, count,
                                                     broker_id, forward=1)
            fd[sym] = fv
        return {"factor_name": factor_name, **factor_correlation(fd, method=method)}

    @mcp.tool()
    async def portfolio_backtest(
        symbols: str, weights_json: str = "", strategy: str = "ma_cross",
        params_json: str = "", initial_capital: float = 1_000_000.0,
        count: int = 250, broker_id: str = "", commission_rate: float = 0.0003,
        stamp_tax: float = 0.001, slippage_bps: float = 5.0,
        execution_timing: str = "close", enforce_limit: bool = True,
        max_participation_pct: float = 1.0, period: str = "1d", rf: float = 0.0,
    ) -> dict:
        """多标的组合回测（N 标的 × 权重矩阵，阶段 3）。

        symbols：逗号分隔；weights_json：权重数组(JSON)，缺省等权；params_json：策略参数(JSON)。
        每个标的独立用统一撮合内核回测，按权重汇总净值；输出可喂回 target_portfolio_sync 的目标持仓。
        """
        import json as _json
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not syms:
            return {"ok": False, "reason": "symbols 不能为空"}
        try:
            weights = _json.loads(weights_json) if weights_json else None
            params = _json.loads(params_json) if params_json else {"fast": 5, "slow": 20}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"JSON 解析失败：{exc}"}
        klines = {}
        for sym in syms:
            res = await fetch_kline_cached(sym, period, count, broker_id=broker_id or None)
            bars = res.get("bars") or []
            if not bars:
                return {"ok": False, "reason": f"{sym} 无历史K线或未连接券商", "symbol": sym}
            klines[sym] = bars
        return run_portfolio_backtest(
            syms, klines, weights, strategy, params, initial_capital,
            commission_rate, stamp_tax, slippage_bps,
            execution_timing=execution_timing, enforce_limit=enforce_limit,
            max_participation_pct=max_participation_pct, period=period, rf=rf)

    @mcp.tool()
    async def walk_forward_analysis(
        symbol: str, strategy: str = "ma_cross", params_json: str = "",
        count: int = 600, broker_id: str = "", commission_rate: float = 0.0003,
        stamp_tax: float = 0.001, slippage_bps: float = 5.0,
        window: int = 120, step: int = 60, period: str = "1d", rf: float = 0.0,
        optimize: bool = False, param_grid_json: str = "",
    ) -> dict:
        """walk-forward 滚动窗口验证（阶段 3）：每个 fold 样本外测试 → 稳健性报告。

        optimize=True 时对每个 train 窗口跑参数寻优（param_grid_json，如 {"fast":[5,10,20]}）。
        返回各 fold 样本外夏普 + 稳健性汇总（夏普分布/参数漂移）。
        """
        import json as _json
        params = _json.loads(params_json) if params_json else {"fast": 5, "slow": 20}
        param_grid = _json.loads(param_grid_json) if param_grid_json else None
        res = await fetch_kline_cached(symbol, period, count, broker_id=broker_id or None)
        bars = res.get("bars") or []
        if not bars:
            return {"ok": False, "reason": f"{symbol} 无历史K线或未连接券商"}
        return walk_forward(
            symbol, bars, strategy, params, 100_000.0, commission_rate, stamp_tax,
            slippage_bps, window=window, step=step, period=period, rf=rf,
            optimize=optimize, param_grid=param_grid)

    @mcp.tool()
    async def attribute_performance(
        symbols: str, broker_id: str = "", period: str = "1d", count: int = 120,
    ) -> dict:
        """绩效归因（阶段 3）：基于真实账户成交 + 真实 K 线，做分标的/分买卖侧/滑点/成本拆解。

        从券商拉取真实成交与 K 线（无假数据）；次根开盘/VWAP 作为滑点参考基准。
        """
        from . import get_bridge
        b = get_bridge(broker_id or None)
        if b is None:
            return {"ok": False, "reason": "未连接券商客户端"}
        deals = await b.call(b.gateway.get_deals)
        if not deals:
            return {"ok": False, "reason": "无成交记录可归因"}
        klines_by_symbol: Dict[str, list] = {}
        for d in deals:
            code = (d.get("code") or "").upper()
            if code and code not in klines_by_symbol:
                kr = await fetch_kline_cached(code, period, count, broker_id=broker_id or None)
                klines_by_symbol[code] = kr.get("bars") or []
        # 成交转 trades 结构（code/side/price/qty/time）
        trades = [{"code": (d.get("code") or "").upper(),
                   "side": "sell" if str(d.get("direction", "")).lower().startswith("s") else "buy",
                   "price": float(d.get("price") or 0),
                   "qty": float(d.get("volume") or 0),
                   "time": d.get("time"),
                   "pnl": float(d.get("profit") or d.get("pnl") or 0)} for d in deals]
        return attribute_pnl(trades, klines_by_symbol)
