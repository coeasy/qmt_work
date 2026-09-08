"""因子组合回测 / walk-forward / 绩效归因（P1-7 / M13）。

拆分自 factor_research.py；复用统一撮合内核（matching）与指标库（metrics）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from xtquant_client.base import BrokerNotConnectedError

from .backtest import _build_cfg, _signals_vectorized, run_param_sweep
from .factor_stats import _mean, _stdev
from .matching import simulate as match_simulate
from .metrics import compute_metrics

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


