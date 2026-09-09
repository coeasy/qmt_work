"""研究深度层（阶段 3）：因子分析 + 组合回测 + walk-forward + 绩效归因。

全部为纯计算（零 mock、不造假数据），输入来自真实 K 线 / 真实账户成交：
- 因子分析：IC / ICIR（滚动截面相关）、分位分组、因子相关性矩阵
- 组合回测：N 标的 × 权重矩阵，复用统一撮合内核；输出可喂回 target_portfolio 的目标持仓
- walk-forward：滚动窗口 train/test + 参数自动寻优（复用 run_param_sweep），输出稳健性报告
- 绩效归因：分标的 / 分策略（买卖侧）/ 滑点 / 成本拆解

MCP 与 REST 仅做「取真实 K 线 → 调用纯函数」的薄封装；降级路径显式标注来源。

P1-7 / M13（2026-09-08）拆分：本文件保留「取数封装 + MCP 工具注册」入口职责，
并 re-export 三组能力以兼容存量 `from tools import factor_research as FR`：
- factor_stats.py     —— 基础统计（叶子层）
- factor_ic.py        —— IC / 分层收益 / 相关性
- factor_backtest.py  —— 组合回测 / walk-forward / 归因
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple  # noqa: F401

from xtquant_client.base import BrokerNotConnectedError

from . import fetch_kline_cached
from .factor_backtest import (  # noqa: F401
    _normalize_weights,
    attribute_pnl,
    run_portfolio_backtest,
    walk_forward,
)
from .factor_ic import (  # noqa: F401
    factor_correlation,
    factor_ic,
    factor_ic_panel,
    ic_statistics,
    quantile_analysis,
)
from .factor_stats import (  # noqa: F401
    _clean_pairs,
    _mean,
    _pearson,
    _rank_avg,
    _spearman,
    _stdev,
)
from .factors import _EXTRA_FIELDS, compute_factor, from_kline

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
