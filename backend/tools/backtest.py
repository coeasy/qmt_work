"""回测引擎：run_backtest / compare_backtests / sensitivity_analysis（真实 K 线，无假数据）。

- K 线来自券商真实历史数据（get_kline），不再使用随机游走
- 策略：ma_cross / macd / rsi（覆盖 QMT-MCP 缺失的 MACD/RSI）
- 指标：总收益 / 年化 / 最大回撤 / 年化波动率 / 夏普 / 交易次数 / 胜率 / 平均盈亏 / VaR
- 覆盖 EzQmt 的「测量效果」对比：compare 多方案并排，sensitivity 参数扫描防过拟合
"""
import math

from .matching import MatchingConfig, simulate as match_simulate
from .metrics import compute_metrics
from xtquant_client.base import BrokerNotConnectedError


# ---------------- 指标 ----------------
def _sma(values: list[float], n: int) -> list[float]:
    out = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(float("nan"))
        else:
            out.append(sum(values[i + 1 - n:i + 1]) / n)
    return out


def _ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _macd(closes: list[float], fast=12, slow=26, signal=9):
    dif = [a - b for a, b in zip(_ema(closes, fast), _ema(closes, slow))]
    dea = _ema(dif, signal)
    hist = [d - e for d, e in zip(dif, dea)]
    return dif, dea, hist


def _rsi(closes: list[float], n=14) -> list[float]:
    out = [float("nan")] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
        if i >= n:
            ag = sum(gains[i - n:i]) / n
            al = sum(losses[i - n:i]) / n
            out[i] = 100 - 100 / (1 + (ag / al if al else 100))
    return out


# ---------------- 信号 ----------------
def _signals(strategy: str, closes: list[float], params: dict) -> list[int]:
    """返回与 closes 等长的仓位信号：1=持有，0=空仓。"""
    n = len(closes)
    sig = [0] * n
    if strategy == "macd":
        _, _, hist = _macd(closes, int(params.get("fast", 12)),
                           int(params.get("slow", 26)), int(params.get("signal", 9)))
        for i in range(1, n):
            if not math.isnan(hist[i]) and not math.isnan(hist[i - 1]):
                sig[i] = 1 if hist[i] > 0 and hist[i - 1] <= 0 else (0 if hist[i] < 0 and hist[i - 1] >= 0 else sig[i - 1])
            else:
                sig[i] = sig[i - 1]
    elif strategy == "rsi":
        p = int(params.get("period", 14))
        r = _rsi(closes, p)
        for i in range(1, n):
            if not math.isnan(r[i]):
                if r[i] < float(params.get("buy", 30)):
                    sig[i] = 1
                elif r[i] > float(params.get("sell", 70)):
                    sig[i] = 0
                else:
                    sig[i] = sig[i - 1]
            else:
                sig[i] = sig[i - 1]
    else:  # ma_cross
        fast = _sma(closes, int(params.get("fast", 5)))
        slow = _sma(closes, int(params.get("slow", 20)))
        for i in range(1, n):
            if math.isnan(fast[i]) or math.isnan(slow[i]):
                sig[i] = sig[i - 1]
            elif fast[i] > slow[i] and (math.isnan(fast[i - 1]) or math.isnan(slow[i - 1])
                                        or fast[i - 1] <= slow[i - 1]):
                sig[i] = 1
            elif fast[i] < slow[i] and (math.isnan(fast[i - 1]) or math.isnan(slow[i - 1])
                                        or fast[i - 1] >= slow[i - 1]):
                sig[i] = 0
            else:
                sig[i] = sig[i - 1]
    return sig


# ---------------- 回测核心 ----------------
def _build_cfg(symbol: str, commission_rate, stamp_tax, slippage_bps, **opt) -> MatchingConfig:
    """由回测参数构造撮合内核配置。"""
    return MatchingConfig(
        execution_timing=opt.get("execution_timing", "close"),
        slippage_bps=float(slippage_bps),
        use_spread_slippage=bool(opt.get("use_spread_slippage", False)),
        max_participation_pct=float(opt.get("max_participation_pct", 1.0)),
        enforce_limit=bool(opt.get("enforce_limit", True)),
        commission_rate=float(commission_rate),
        stamp_tax=float(stamp_tax),
        code=symbol,
        is_st=bool(opt.get("is_st", False)),
    )


def _split_metrics(equity: list[float], trades: list[dict], train_ratio: float,
                   period: str = "1d", rf: float = 0.0):
    """样本内/外指标切分：按 train_ratio 切净值序列；无切分返回 (None, None)。"""
    if train_ratio is None or train_ratio >= 1.0:
        return None, None
    k = max(2, int(len(equity) * train_ratio))
    if k >= len(equity) - 1:
        return None, None
    tr_equity = equity[:k + 1]
    te_equity = equity[k:]
    # 成交按时间顺序近似归属样本内/外（前 k 笔视为样本内）
    tr_trades = trades[:k] if trades else []
    te_trades = trades[k:] if trades else []
    train = compute_metrics(tr_equity, tr_trades, period=period, rf=rf)
    test = compute_metrics(te_equity, te_trades, period=period, rf=rf)
    return train, test


def run_backtest_engine(symbol: str, kline: list[dict], strategy: str,
                        params: dict, initial_capital: float,
                        commission_rate: float = 0.0003,
                        stamp_tax: float = 0.001,
                        slippage_bps: float = 5.0,
                        *, execution_timing: str = "close",
                        enforce_limit: bool = True,
                        max_participation_pct: float = 1.0,
                        use_spread_slippage: bool = False,
                        period: str = "1d", is_st: bool = False,
                        train_ratio: float = 1.0, rf: float = 0.0) -> dict:
    """基于真实 K 线运行回测（多头、满仓切换），含 A 股规则感知的撮合内核。

    成本模型：买入价上浮滑点、卖出价下浮滑点；佣金双边（默认万 3），
    印花税卖出（默认千 1）；成本直接扣除现金。
    撮合内核 (`tools/matching`) 支持：执行时点(close/next_open)、滑点模型、
    涨跌停不可成交、成交量容量约束、整手。
    train_ratio<1 时额外输出样本内/外指标对比（防过拟合基线）。
    """
    closes = [b["close"] for b in kline if b.get("close") is not None]
    if len(closes) < 30:
        raise BrokerNotConnectedError(f"{symbol} K 线不足（需≥30 根），请确认券商已返回历史数据。")
    sig = _signals(strategy, closes, params)
    cfg = _build_cfg(symbol, commission_rate, stamp_tax, slippage_bps,
                     execution_timing=execution_timing, enforce_limit=enforce_limit,
                     max_participation_pct=max_participation_pct,
                     use_spread_slippage=use_spread_slippage, is_st=is_st)
    sim = match_simulate(closes, sig, kline, cfg, initial_capital)
    equity, trades = sim["equity"], sim["trades"]
    metrics = compute_metrics(equity, trades, period=period, rf=rf)
    train_m, test_m = _split_metrics(equity, trades, train_ratio, period, rf=rf)
    out = {"symbol": symbol, "strategy": strategy, "params": params,
           "initial_capital": initial_capital, "metrics": metrics,
           "trades": trades[-20:], "trade_count": len(trades),
           "equity_curve": [round(e, 2) for e in equity[-200:]],
           "cost_model": {"commission_rate": commission_rate,
                          "stamp_tax": stamp_tax, "slippage_bps": slippage_bps,
                          "execution_timing": execution_timing,
                          "enforce_limit": enforce_limit,
                          "max_participation_pct": max_participation_pct},
           "engine": "legacy"}
    if train_m:
        out["train_test"] = {"train_ratio": train_ratio,
                             "train": train_m, "test": test_m}
    return out


async def fetch_kline_async(broker_id: str, symbol: str, count: int = 250) -> list[dict]:
    """回测取历史日线：C1 缓存优先（命中则不穿透券商，断线时用本地真实历史兜底）。"""
    from . import fetch_kline_cached
    res = await fetch_kline_cached(symbol, "1d", count, broker_id=broker_id or None)
    return res.get("bars") or []


def register_backtest_tools(mcp):
    @mcp.tool()
    async def run_backtest(
        symbol: str,
        strategy: str = "ma_cross",
        params: dict = None,
        initial_capital: float = 100_000.0,
        count: int = 250,
        commission_rate: float = 0.0003,
        stamp_tax: float = 0.001,
        slippage_bps: float = 5.0,
        execution_timing: str = "close",
        enforce_limit: bool = True,
        max_participation_pct: float = 1.0,
        period: str = "1d",
        train_ratio: float = 1.0,
        rf: float = 0.0,
        broker_id: str = "",
    ) -> dict:
        """运行一次回测（真实 K 线 + 策略信号 + A 股规则撮合内核），返回指标 + 成交明细 + 评级。

        cost 参数：commission_rate 佣金（默认万3）、stamp_tax 印花税（默认千1，卖出）、
        slippage_bps 单边滑点（默认 5bp）。
        撮合参数：execution_timing(close/next_open)、enforce_limit(涨停买不进/跌停卖不出)、
        max_participation_pct(单根成交量参与率上限)、period(bar 频率，决定年化乘子)、
        train_ratio(<1 时输出样本内/外对比防过拟合)、rf(无风险年化收益率)。
        """
        params = params or {"fast": 5, "slow": 20}
        kline = await fetch_kline_async(broker_id, symbol, count)
        return run_backtest_engine(symbol, kline, strategy, params, initial_capital,
                                   commission_rate, stamp_tax, slippage_bps,
                                   execution_timing=execution_timing,
                                   enforce_limit=enforce_limit,
                                   max_participation_pct=max_participation_pct,
                                   period=period, train_ratio=train_ratio, rf=rf)

    @mcp.tool()
    async def compare_backtests(configs: list[dict], broker_id: str = "") -> dict:
        """多方案横向对比（§4.7）：一组回测配置 -> 指标矩阵并排。"""
        rows = []
        for cfg in configs:
            symbol = cfg.get("symbol", "600519.SH")
            kline = await fetch_kline_async(broker_id, symbol, int(cfg.get("count", 250)))
            res = run_backtest_engine(symbol, kline, cfg.get("strategy", "ma_cross"),
                                      cfg.get("params", {"fast": 5, "slow": 20}),
                                      float(cfg.get("initial_capital", 100_000)))
            rows.append({"config": cfg, "metrics": res["metrics"]})
        return {"rows": sorted(rows, key=lambda r: r["metrics"].get("sharpe", 0), reverse=True)}

    @mcp.tool()
    async def sensitivity_analysis(
        symbol: str = "600519.SH",
        param: str = "fast",
        values: list = None,
        broker_id: str = "",
    ) -> dict:
        """参数敏感性扫描（§4.7）：扫描单参数 -> 指标变化（防过拟合、找稳健区间）。"""
        values = values or [3, 5, 10, 20, 30]
        table = []
        base = {"fast": 5, "slow": 20}
        for v in values:
            kline = await fetch_kline_async(broker_id, symbol, 250)
            p = dict(base); p[param] = v
            res = run_backtest_engine(symbol, kline, "ma_cross", p, 100_000.0)
            m = res["metrics"]
            table.append({"param": v, "sharpe": m.get("sharpe"),
                          "max_drawdown": m.get("max_drawdown"),
                          "total_return": m.get("total_return")})
        return {"symbol": symbol, "param": param, "table": table}


# ============================================================================
# P1 向量化回测引擎 + 参数扫描（grid search）
# ============================================================================
import itertools
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None


def _signals_vectorized(strategy: str, closes, params: dict) -> list[int]:
    """向量化生成仓位信号（1=持有 / 0=空仓），pandas/numpy 加速。

    与 `_signals` 语义严格一致：
    - 状态携带（carry）：无穿越时维持上一根信号；
    - ma_cross：当前柱无效（NaN）则携带；前一周期无效或等于边界视为「穿越触发」；
    - macd：仅当当前与前一柱均有效、且 hist 符号翻转时穿越；
    - rsi：阈值触发（<buy 持有、>sell 空仓），无效柱携带。

    实现要点：以 NaN 为基底，仅在穿越点显式置 1/0，再用 ffill 携带——规避
    旧版 `.where(...).where(...)` 把中性柱置 0 导致无法携带上一状态的缺陷。
    """
    if pd is not None and np is not None:
        s = pd.Series(closes, dtype="float64")
        sig = pd.Series(np.nan, index=s.index, dtype="float64")
        if strategy == "macd":
            fast = int(params.get("fast", 12)); slow = int(params.get("slow", 26))
            sig_n = int(params.get("signal", 9))
            dif = s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()
            dea = dif.ewm(span=sig_n, adjust=False).mean()
            hist = dif - dea
            cur_valid = ~hist.isna()
            prev_valid = ~hist.shift(1).isna()
            cross_up = cur_valid & prev_valid & (hist > 0) & (hist.shift(1) <= 0)
            cross_dn = cur_valid & prev_valid & (hist < 0) & (hist.shift(1) >= 0)
            sig = sig.where(~cross_up, 1.0).where(~cross_dn, 0.0)
        elif strategy == "rsi":
            p = int(params.get("period", 14))
            r = _rsi_vectorized(s, p)   # 无效柱返回 NaN -> 携带
            buy = float(params.get("buy", 30)); sell = float(params.get("sell", 70))
            valid = ~r.isna()
            up = valid & (r < buy)
            dn = valid & (r > sell)
            sig = sig.where(~up, 1.0).where(~dn, 0.0)
        else:  # ma_cross
            fast = int(params.get("fast", 5)); slow = int(params.get("slow", 20))
            fa = s.rolling(fast).mean(); sa = s.rolling(slow).mean()
            cur_valid = ~(fa.isna() | sa.isna())
            cur_above = (fa > sa)
            cur_below = (fa < sa)
            prev_invalid = fa.shift(1).isna() | sa.shift(1).isna()
            prev_le = (fa.shift(1) <= sa.shift(1))
            prev_ge = (fa.shift(1) >= sa.shift(1))
            cross_up = cur_valid & cur_above & (prev_invalid | prev_le)
            cross_dn = cur_valid & cur_below & (prev_invalid | prev_ge)
            sig = sig.where(~cross_up, 1.0).where(~cross_dn, 0.0)
        sig = sig.ffill().fillna(0.0).astype("int64")
        return [int(x) for x in sig.tolist()]
    # 回退：纯 Python（无 pandas 时）
    return _signals(strategy, list(closes), params)


def _rsi_vectorized(s: "pd.Series", n: int) -> "pd.Series":
    """向量化 RSI：与 `_rsi` 严格一致（窗口不足返回 NaN；al==0 时 ratio 取 100）。

    注意：legacy `_rsi` 在 al==0 时写死 `ag/al if al else 100`，故全涨窗口 RSI≈99.0099
    而非 NaN，这里用 `.where` 精确复刻该分支，避免信号穿越语义偏差。返回 Series。
    """
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = (gain / loss).where(loss != 0, 100.0)
    return 100 - 100 / (1 + ratio)  # 窗口不足处为 NaN -> 信号携带


def _simulate(closes: list[float], sig: list[int], kline: list[dict],
              initial_capital: float, commission_rate: float,
              stamp_tax: float, slippage_bps: float,
              cfg: MatchingConfig | None = None, symbol: str = "",
              period: str = "1d", train_ratio: float = 1.0,
              rf: float = 0.0) -> dict:
    """统一撮合内核封装（向量化信号下的回测）。"""
    if cfg is None:
        cfg = MatchingConfig(commission_rate=float(commission_rate),
                             stamp_tax=float(stamp_tax),
                             slippage_bps=float(slippage_bps), code=symbol)
    sim = match_simulate(closes, sig, kline, cfg, initial_capital)
    equity, trades = sim["equity"], sim["trades"]
    metrics = compute_metrics(equity, trades, period=period, rf=rf)
    train_m, test_m = _split_metrics(equity, trades, train_ratio, period, rf=rf)
    return {"equity": equity, "trades": trades, "metrics": metrics,
            "train": train_m, "test": test_m}


def run_backtest_vectorized(symbol: str, kline: list[dict], strategy: str,
                            params: dict, initial_capital: float,
                            commission_rate: float = 0.0003,
                            stamp_tax: float = 0.001,
                            slippage_bps: float = 5.0,
                            *, execution_timing: str = "close",
                            enforce_limit: bool = True,
                            max_participation_pct: float = 1.0,
                            use_spread_slippage: bool = False,
                            period: str = "1d", is_st: bool = False,
                            train_ratio: float = 1.0, rf: float = 0.0) -> dict:
    """向量化回测（pandas/numpy 指标 + 统一交易内核），输出形状与 run_backtest_engine 一致。"""
    closes = [b["close"] for b in kline if b.get("close") is not None]
    if len(closes) < 30:
        raise BrokerNotConnectedError(f"{symbol} K 线不足（需≥30 根），请确认券商已返回历史数据。")
    sig = _signals_vectorized(strategy, closes, params)
    cfg = _build_cfg(symbol, commission_rate, stamp_tax, slippage_bps,
                     execution_timing=execution_timing, enforce_limit=enforce_limit,
                     max_participation_pct=max_participation_pct,
                     use_spread_slippage=use_spread_slippage, is_st=is_st)
    sim = _simulate(closes, sig, kline, initial_capital, commission_rate, stamp_tax,
                    slippage_bps, cfg=cfg, symbol=symbol, period=period,
                    train_ratio=train_ratio, rf=rf)
    out = {"symbol": symbol, "strategy": strategy, "params": params,
           "initial_capital": initial_capital, "metrics": sim["metrics"],
           "trades": sim["trades"][-20:], "trade_count": len(sim["trades"]),
           "equity_curve": [round(e, 2) for e in sim["equity"][-200:]],
           "cost_model": {"commission_rate": commission_rate,
                          "stamp_tax": stamp_tax, "slippage_bps": slippage_bps,
                          "execution_timing": execution_timing,
                          "enforce_limit": enforce_limit,
                          "max_participation_pct": max_participation_pct},
           "engine": "vectorized"}
    if sim["train"]:
        out["train_test"] = {"train_ratio": train_ratio,
                             "train": sim["train"], "test": sim["test"]}
    return out


def run_param_sweep(symbol: str, kline: list[dict], strategy: str,
                    param_grid: dict, initial_capital: float = 100_000.0,
                    commission_rate: float = 0.0003, stamp_tax: float = 0.001,
                    slippage_bps: float = 5.0) -> dict:
    """参数网格扫描（grid search）：穷举参数组合 -> 指标矩阵 -> 按夏普排序选优。

    param_grid 形如 {"fast":[5,10,20], "slow":[20,40]}；返回最优组合与全量网格。
    """
    if not param_grid:
        raise ValueError("param_grid 不能为空")
    names = list(param_grid.keys())
    value_lists = [param_grid[k] for k in names]
    grid = []
    best = None
    for combo in itertools.product(*value_lists):
        params = {k: (int(v) if isinstance(v, float) and v.is_integer() else v)
                  for k, v in zip(names, combo)}
        try:
            res = run_backtest_vectorized(symbol, kline, strategy, params,
                                          initial_capital, commission_rate,
                                          stamp_tax, slippage_bps)
        except (BrokerNotConnectedError, ValueError):
            continue
        m = res["metrics"]
        grid.append({"params": params, "metrics": m})
        if best is None or (m.get("sharpe", -99) > best["metrics"].get("sharpe", -99)):
            best = {"params": params, "metrics": m}
    grid.sort(key=lambda r: r["metrics"].get("sharpe", -99), reverse=True)
    return {"symbol": symbol, "strategy": strategy, "count": len(grid),
            "best": best, "grid": grid}
