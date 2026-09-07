"""回测引擎：run_backtest / compare_backtests / sensitivity_analysis（真实 K 线，无假数据）。

- K 线来自券商真实历史数据（get_kline），不再使用随机游走
- 策略：ma_cross / macd / rsi（覆盖 QMT-MCP 缺失的 MACD/RSI）
- 指标：总收益 / 年化 / 最大回撤 / 年化波动率 / 夏普 / 交易次数 / 胜率 / 平均盈亏 / VaR
- 覆盖 EzQmt 的「测量效果」对比：compare 多方案并排，sensitivity 参数扫描防过拟合

指标实现统一从 ``tools.indicators`` 取（2026-09-07 重构 R2），避免与
``tools.strategy_runtime`` 重复实现。
"""
from xtquant_client.base import BrokerNotConnectedError

from .indicators import signals_for
from .matching import MatchingConfig
from .matching import simulate as match_simulate
from .metrics import compute_metrics


def _signals(strategy: str, closes: list[float], params: dict) -> list[int]:
    """返回与 closes 等长的仓位信号：1=持有，0=空仓。

    委托给统一的 tools.indicators.signals_for（2026-09-07 抽取）。
    保留此函数为兼容旧调用方。
    """
    return [int(x) for x in signals_for(strategy, closes, params).tolist()]


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
                        train_ratio: float = 1.0, rf: float = 0.0,
                        data_meta: dict | None = None) -> dict:
    """基于真实 K 线运行回测（多头、满仓切换），含 A 股规则感知的撮合内核。

    成本模型：买入价上浮滑点、卖出价下浮滑点；佣金双边（默认万 3），
    印花税卖出（默认千 1）；成本直接扣除现金。
    撮合内核 (`tools/matching`) 支持：执行时点(close/next_open)、滑点模型、
    涨跌停不可成交、成交量容量约束、整手。
    train_ratio<1 时额外输出样本内/外指标对比（防过拟合基线）。
    """
    # 只保留有收盘价的 bar：撮合内核按索引访问 kline，长度若与 closes 不一致会整体错位
    bars = [b for b in kline if b.get("close") is not None]
    closes = [b["close"] for b in bars]
    dates = [b.get("date") or b.get("datetime") or b.get("time") for b in bars]
    if len(closes) < 30:
        raise BrokerNotConnectedError(f"{symbol} K 线不足（需≥30 根），请确认券商已返回历史数据。")
    sig = _signals(strategy, closes, params)
    cfg = _build_cfg(symbol, commission_rate, stamp_tax, slippage_bps,
                     execution_timing=execution_timing, enforce_limit=enforce_limit,
                     max_participation_pct=max_participation_pct,
                     use_spread_slippage=use_spread_slippage, is_st=is_st)
    sim = match_simulate(closes, sig, bars, cfg, initial_capital)
    equity, trades = sim["equity"], sim["trades"]
    metrics = compute_metrics(equity, trades, period=period, rf=rf, dates=dates)
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
    _annotate_provenance(out, bars, data_meta)
    _annotate_no_trade(out, closes, sig, cfg, initial_capital, trades)
    if train_m:
        out["train_test"] = {"train_ratio": train_ratio,
                             "train": train_m, "test": test_m}
    return out


def _annotate_provenance(out: dict, bars: list[dict], data_meta: dict | None) -> None:
    """G1 诚信标注：回测结论必须带数据来源与时效。

    缓存/降级缓存跑出的结果不等于券商实时数据，必须显式标注 stale + as_of，
    让用户知道这份结论「截止到哪一天、是不是降级来的」。
    """
    if not data_meta:
        return
    out["data_source"] = data_meta
    src = data_meta.get("source")
    if src in ("cache", "cache_stale"):
        out["stale"] = True
        out["as_of"] = (bars[-1].get("date") or bars[-1].get("time")) if bars else None
        if src == "cache_stale":
            out["data_source"]["stale_reason"] = data_meta.get("note") or "券商不可达，回退本地历史缓存"


def _annotate_no_trade(out: dict, closes: list[float], sig: list[int],
                       cfg, initial_capital: float, trades: list) -> None:
    """0 成交时必须说清是「没信号」还是「买不起一手」。

    否则用户会把「本金不够买 1 手茅台」误读成「策略不赚钱」——这是最危险的假结论。
    """
    if trades:
        return
    if not any(sig):
        out["diagnostics"] = "策略在该区间未产生任何持仓信号（可尝试调整参数或拉长区间）"
        return
    hold_px = [closes[i] for i in range(min(len(closes), len(sig))) if sig[i] == 1]
    if not hold_px:
        out["diagnostics"] = "策略在该区间未产生任何持仓信号（可尝试调整参数或拉长区间）"
        return
    min_px = min(hold_px)
    need = min_px * cfg.min_lot
    if need > initial_capital:
        out["diagnostics"] = (
            f"初始资金不足，全程无法建仓：持仓信号期间最低价 {min_px:.2f} 元，"
            f"买入 1 手（{cfg.min_lot} 股）约需 {need:,.0f} 元，"
            f"而初始资金仅 {initial_capital:,.0f} 元。请提高初始资金或改选低价标的。")
    else:
        out["diagnostics"] = (
            "产生了持仓信号但无成交：可能被「涨停买不进/跌停卖不出」或成交量容量约束拦截；"
            "可尝试放宽 max_participation_pct 或关闭 enforce_limit 复测。")


async def fetch_kline_async(broker_id: str, symbol: str, count: int = 250) -> list[dict]:
    """回测取历史日线：C1 缓存优先（命中则不穿透券商，断线时用本地真实历史兜底）。"""
    from . import fetch_kline_cached
    res = await fetch_kline_cached(symbol, "1d", count, broker_id=broker_id or None)
    return res.get("bars") or []


async def fetch_kline_async_meta(broker_id: str, symbol: str,
                                 count: int = 250) -> tuple[list[dict], dict]:
    """同 fetch_kline_async，但连数据来源元信息一起返回。

    G1 诚信要求：回测结论必须说清数据来自「券商实时 / 本地缓存 / 降级缓存」，
    否则用户会把「用上周缓存跑出的结果」当成今天的决策依据而不自知。
    """
    from . import fetch_kline_cached
    res = await fetch_kline_cached(symbol, "1d", count, broker_id=broker_id or None)
    bars = res.get("bars") or []
    meta = {"source": res.get("source"), "cached_at": res.get("cached_at")}
    if res.get("note"):
        meta["note"] = res["note"]
    return bars, meta


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
        kline, meta = await fetch_kline_async_meta(broker_id, symbol, count)
        return run_backtest_engine(symbol, kline, strategy, params, initial_capital,
                                   commission_rate, stamp_tax, slippage_bps,
                                   execution_timing=execution_timing,
                                   enforce_limit=enforce_limit,
                                   max_participation_pct=max_participation_pct,
                                   period=period, train_ratio=train_ratio, rf=rf,
                                   data_meta=meta)

    @mcp.tool()
    async def compare_backtests(configs: list[dict], broker_id: str = "") -> dict:
        """多方案横向对比（§4.7）：一组回测配置 -> 指标矩阵并排。

        P2-4：把每个 config 内的成本与撮合参数（commission_rate/stamp_tax/
        slippage_bps/execution_timing/enforce_limit）透传给回测引擎，保证
        跨成本方案对比基准一致、不与默认值混淆。
        """
        rows = []
        for cfg in configs:
            symbol = cfg.get("symbol", "600519.SH")
            kline, meta = await fetch_kline_async_meta(broker_id, symbol, int(cfg.get("count", 250)))
            res = run_backtest_engine(symbol, kline, cfg.get("strategy", "ma_cross"),
                                      cfg.get("params", {"fast": 5, "slow": 20}),
                                      float(cfg.get("initial_capital", 100_000)),
                                      commission_rate=float(cfg.get("commission_rate", 0.0003)),
                                      stamp_tax=float(cfg.get("stamp_tax", 0.001)),
                                      slippage_bps=float(cfg.get("slippage_bps", 5.0)),
                                      execution_timing=cfg.get("execution_timing", "close"),
                                      enforce_limit=bool(cfg.get("enforce_limit", True)),
                                      data_meta=meta)
            rows.append({"config": cfg, "metrics": res["metrics"],
                         "stale": res.get("stale", False), "as_of": res.get("as_of")})
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
        stale = False
        as_of = None
        for v in values:
            kline, meta = await fetch_kline_async_meta(broker_id, symbol, 250)
            p = dict(base); p[param] = v
            res = run_backtest_engine(symbol, kline, "ma_cross", p, 100_000.0, data_meta=meta)
            stale = stale or bool(res.get("stale"))
            as_of = res.get("as_of") or as_of
            m = res["metrics"]
            table.append({"param": v, "sharpe": m.get("sharpe"),
                          "max_drawdown": m.get("max_drawdown"),
                          "total_return": m.get("total_return")})
        # 敏感性扫描整表共用同一份数据，数据时效标注放在外层
        return {"symbol": symbol, "param": param, "table": table,
                "stale": stale, "as_of": as_of}


# ============================================================================
# P1 向量化回测引擎 + 参数扫描（grid search）
# ============================================================================
import itertools  # noqa: E402 —— 文件中部引入（模块级说明段之后，约定保留）

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None


def _signals_vectorized(strategy: str, closes, params: dict) -> list[int]:
    """向量化生成仓位信号（1=持有 / 0=空仓），统一委托给 tools.indicators。

    历史背景：原实现独立维护 pandas EMA/RSI 公式（_signals_vectorized + _rsi_vectorized），
    与 tools/strategy_runtime 的 numpy 版本语义微妙不同。2026-09-07 重构 R2 收敛为单一实现。
    向量化优势（pandas/numpy）由 tools.indicators 内部 numpy 数组保证，无需本文件再写。
    """
    return [int(x) for x in signals_for(strategy, list(closes), params).tolist()]


# 旧的 _rsi_vectorized 已废弃（2026-09-07 R2 统一到 tools.indicators.rsi），
# 保留为薄包装避免破坏可能的外部 import：
def _rsi_vectorized(s, n: int):
    """deprecated: 委托给 tools.indicators.rsi（输入可为 list/Series，统一转 np.array）"""
    from .indicators import rsi as _rsi
    return _rsi(list(s) if hasattr(s, "tolist") else s, n)


def _simulate(closes: list[float], sig: list[int], kline: list[dict],
              initial_capital: float, commission_rate: float,
              stamp_tax: float, slippage_bps: float,
              cfg: MatchingConfig | None = None, symbol: str = "",
              period: str = "1d", train_ratio: float = 1.0,
              rf: float = 0.0, dates: list | None = None) -> dict:
    """统一撮合内核封装（向量化信号下的回测）。"""
    if cfg is None:
        cfg = MatchingConfig(commission_rate=float(commission_rate),
                             stamp_tax=float(stamp_tax),
                             slippage_bps=float(slippage_bps), code=symbol)
    sim = match_simulate(closes, sig, kline, cfg, initial_capital)
    equity, trades = sim["equity"], sim["trades"]
    metrics = compute_metrics(equity, trades, period=period, rf=rf, dates=dates)
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
                            train_ratio: float = 1.0, rf: float = 0.0,
                            data_meta: dict | None = None) -> dict:
    """向量化回测（pandas/numpy 指标 + 统一交易内核），输出形状与 run_backtest_engine 一致。"""
    bars = [b for b in kline if b.get("close") is not None]
    closes = [b["close"] for b in bars]
    dates = [b.get("date") or b.get("datetime") or b.get("time") for b in bars]
    if len(closes) < 30:
        raise BrokerNotConnectedError(f"{symbol} K 线不足（需≥30 根），请确认券商已返回历史数据。")
    sig = _signals_vectorized(strategy, closes, params)
    cfg = _build_cfg(symbol, commission_rate, stamp_tax, slippage_bps,
                     execution_timing=execution_timing, enforce_limit=enforce_limit,
                     max_participation_pct=max_participation_pct,
                     use_spread_slippage=use_spread_slippage, is_st=is_st)
    sim = _simulate(closes, sig, bars, initial_capital, commission_rate, stamp_tax,
                    slippage_bps, cfg=cfg, symbol=symbol, period=period,
                    train_ratio=train_ratio, rf=rf, dates=dates)
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
    # 与 legacy 引擎口径一致：数据来源时效标注 + 0 成交原因说明
    _annotate_provenance(out, bars, data_meta)
    _annotate_no_trade(out, closes, sig, cfg, initial_capital, sim["trades"])
    if sim["train"]:
        out["train_test"] = {"train_ratio": train_ratio,
                             "train": sim["train"], "test": sim["test"]}
    return out


def run_param_sweep(symbol: str, kline: list[dict], strategy: str,
                    param_grid: dict, initial_capital: float = 100_000.0,
                    commission_rate: float = 0.0003, stamp_tax: float = 0.001,
                    slippage_bps: float = 5.0,
                    *, execution_timing: str = "close",
                    enforce_limit: bool = True) -> dict:
    """参数网格扫描（grid search）：穷举参数组合 -> 指标矩阵 -> 按夏普排序选优。

    param_grid 形如 {"fast":[5,10,20], "slow":[20,40]}；返回最优组合与全量网格。
    P2-4：成本与撮合参数透传（execution_timing/enforce_limit 补上），
    保证扫描全程基准一致。
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
                                          stamp_tax, slippage_bps,
                                          execution_timing=execution_timing,
                                          enforce_limit=enforce_limit)
        except (BrokerNotConnectedError, ValueError):
            continue
        m = res["metrics"]
        grid.append({"params": params, "metrics": m})
        if best is None or (m.get("sharpe", -99) > best["metrics"].get("sharpe", -99)):
            best = {"params": params, "metrics": m}
    grid.sort(key=lambda r: r["metrics"].get("sharpe", -99), reverse=True)
    return {"symbol": symbol, "strategy": strategy, "count": len(grid),
            "best": best, "grid": grid}
