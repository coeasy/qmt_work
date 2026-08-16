"""研究深度层路由（阶段 3）：因子分析 / 组合回测 / walk-forward / 绩效归因。

- POST /research/factor-ic      因子 IC / ICIR（单序列 或 截面 panel）
- POST /research/quantile       分位（分位数）分组分析
- POST /research/correlation    因子相关性矩阵（跨标的同因子，或直传 factor_dict）
- POST /research/portfolio-backtest  多标的组合回测（N×权重，闭环到 target_portfolio）
- POST /research/walk-forward   walk-forward 滚动窗口 + 参数寻优
- POST /research/attribution    绩效归因（分标的/分买卖侧/滑点/成本）

标签前缀统一为 /research；由集成方挂载到 /api/v1。所有行情来自真实券商 K 线。
"""
from typing import Any, Dict

from app.routes._common import ok, err
from fastapi import APIRouter

from tools import factor_research as FR
from tools.factor_research import (
    attribute_pnl,
    factor_correlation,
    factor_ic,
    factor_ic_panel,
    ic_statistics,
    quantile_analysis,
    run_portfolio_backtest,
    walk_forward,
)
from xtquant_client.base import BrokerError

router = APIRouter()


@router.post("/research/factor-ic")
async def post_factor_ic(body: Dict[str, Any]):
    """因子 IC / ICIR。

    body: {symbol, factor_name?, period?, count?, broker_id?, mode?("series"|"panel"),
           method?("pearson"|"spearman"), forward?}
    mode=panel 时 symbol 为逗号分隔的多标的（截面逐期 IC → ICIR）。
    """
    symbol = body.get("symbol")
    if not symbol:
        return err(400, "缺少 symbol")
    factor_name = body.get("factor_name") or "rsi"
    period = body.get("period") or "1d"
    count = int(body.get("count") or 250)
    broker_id = body.get("broker_id") or None
    mode = body.get("mode") or "series"
    method = body.get("method") or "pearson"
    forward = int(body.get("forward") or 1)
    try:
        if mode == "panel":
            symbols = [s.strip().upper() for s in symbol.split(",") if s.strip()]
            panels_f, panels_r, src = [], [], None
            for sym in symbols:
                fv, fwd, s = await FR._factor_and_forward(sym, factor_name, period, count,
                                                          broker_id, forward)
                panels_f.append(fv); panels_r.append(fwd); src = s
            ic_list = factor_ic_panel(panels_f, panels_r, method=method)
            return ok({"symbols": symbols, "factor_name": factor_name, "mode": "panel",
                       "method": method, "forward": forward, "source": src,
                       "ic_series": [None if v is None else round(v, 4) for v in ic_list],
                       "stats": ic_statistics(ic_list)})
        fv, fwd, src = await FR._factor_and_forward(symbol, factor_name, period, count,
                                                    broker_id, forward)
        ic = factor_ic(fv, fwd, method=method)
        return ok({"symbol": symbol, "factor_name": factor_name, "mode": "series",
                   "method": method, "forward": forward, "source": src,
                   "ic": round(ic, 4) if ic is not None else None})
    except BrokerError as exc:
        return err(503, str(exc))
    except ValueError as exc:
        return err(400, str(exc))


@router.post("/research/quantile")
async def post_quantile(body: Dict[str, Any]):
    """分位分组分析。body: {symbol, factor_name?, period?, count?, broker_id?, forward?, n_q?}。"""
    symbol = body.get("symbol")
    if not symbol:
        return err(400, "缺少 symbol")
    factor_name = body.get("factor_name") or "rsi"
    period = body.get("period") or "1d"
    count = int(body.get("count") or 250)
    broker_id = body.get("broker_id") or None
    forward = int(body.get("forward") or 1)
    n_q = int(body.get("n_q") or 5)
    try:
        fv, fwd, src = await FR._factor_and_forward(symbol, factor_name, period, count,
                                                    broker_id, forward)
        out = quantile_analysis(fv, fwd, n_q=n_q)
        out.update({"symbol": symbol, "factor_name": factor_name,
                     "forward": forward, "source": src})
        return ok(out)
    except BrokerError as exc:
        return err(503, str(exc))
    except ValueError as exc:
        return err(400, str(exc))


@router.post("/research/correlation")
async def post_correlation(body: Dict[str, Any]):
    """因子相关性矩阵。

    两种输入：
    1) 跨标的同因子：{symbols(逗号分隔), factor_name?, period?, count?, broker_id?, method?}
    2) 直传多列：{factor_dict:{name: [..]}, method?}
    """
    method = body.get("method") or "pearson"
    factor_dict = body.get("factor_dict")
    try:
        if factor_dict:
            return ok(factor_correlation(factor_dict, method=method))
        symbols = body.get("symbols")
        if not symbols:
            return err(400, "缺少 symbols 或 factor_dict")
        factor_name = body.get("factor_name") or "rsi"
        period = body.get("period") or "1d"
        count = int(body.get("count") or 250)
        broker_id = body.get("broker_id") or None
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        fd: Dict[str, list] = {}
        for sym in syms:
            fv, _fwd, _s = await FR._factor_and_forward(sym, factor_name, period, count,
                                                        broker_id, forward=1)
            fd[sym] = fv
        return ok({"factor_name": factor_name, **factor_correlation(fd, method=method)})
    except BrokerError as exc:
        return err(503, str(exc))
    except ValueError as exc:
        return err(400, str(exc))


@router.post("/research/portfolio-backtest")
async def post_portfolio_backtest(body: Dict[str, Any]):
    """多标的组合回测。body: {symbols(逗号), weights_json?, strategy?, params_json?, ...}。"""
    import json as _json
    raw = body.get("symbols")
    if not raw:
        return err(400, "缺少 symbols")
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    weights = _json.loads(body["weights_json"]) if body.get("weights_json") else None
    params = _json.loads(body["params_json"]) if body.get("params_json") else {"fast": 5, "slow": 20}
    period = body.get("period") or "1d"
    count = int(body.get("count") or 250)
    broker_id = body.get("broker_id") or None
    try:
        klines = {}
        for sym in syms:
            res = await FR.fetch_kline_cached(sym, period, count, broker_id=broker_id)
            bars = res.get("bars") or []
            if not bars:
                return err(503, f"{sym} 无历史K线或未连接券商")
            klines[sym] = bars
        out = run_portfolio_backtest(
            syms, klines, weights, body.get("strategy", "ma_cross"), params,
            float(body.get("initial_capital", 1_000_000.0)),
            float(body.get("commission_rate", 0.0003)),
            float(body.get("stamp_tax", 0.001)),
            float(body.get("slippage_bps", 5.0)),
            execution_timing=body.get("execution_timing", "close"),
            enforce_limit=bool(body.get("enforce_limit", True)),
            max_participation_pct=float(body.get("max_participation_pct", 1.0)),
            period=period, rf=float(body.get("rf", 0.0)))
        return ok(out)
    except BrokerError as exc:
        return err(503, str(exc))
    except ValueError as exc:
        return err(400, str(exc))


@router.post("/research/walk-forward")
async def post_walk_forward(body: Dict[str, Any]):
    """walk-forward 滚动窗口验证。body: {symbol, strategy?, params_json?, count?, window?, step?, optimize?, param_grid_json?}。"""
    import json as _json
    symbol = body.get("symbol")
    if not symbol:
        return err(400, "缺少 symbol")
    params = _json.loads(body["params_json"]) if body.get("params_json") else {"fast": 5, "slow": 20}
    param_grid = _json.loads(body["param_grid_json"]) if body.get("param_grid_json") else None
    period = body.get("period") or "1d"
    count = int(body.get("count") or 600)
    broker_id = body.get("broker_id") or None
    try:
        res = await FR.fetch_kline_cached(symbol, period, count, broker_id=broker_id)
        bars = res.get("bars") or []
        if not bars:
            return err(503, f"{symbol} 无历史K线或未连接券商")
        out = walk_forward(
            symbol, bars, body.get("strategy", "ma_cross"), params, 100_000.0,
            float(body.get("commission_rate", 0.0003)),
            float(body.get("stamp_tax", 0.001)),
            float(body.get("slippage_bps", 5.0)),
            window=int(body.get("window", 120)), step=int(body.get("step", 60)),
            period=period, rf=float(body.get("rf", 0.0)),
            optimize=bool(body.get("optimize", False)), param_grid=param_grid)
        return ok(out)
    except BrokerError as exc:
        return err(503, str(exc))
    except ValueError as exc:
        return err(400, str(exc))


@router.post("/research/attribution")
async def post_attribution(body: Dict[str, Any]):
    """绩效归因（需券商连接 + 成交）。body: {broker_id?, period?, count?}。

    也可直传成交：{trades:[...], klines_by_symbol:{code:[bars]}} 走纯计算（无券商依赖）。
    """
    trades = body.get("trades")
    klines_by_symbol = body.get("klines_by_symbol")
    if trades is not None and klines_by_symbol is not None:
        try:
            return ok(attribute_pnl(trades, klines_by_symbol))
        except ValueError as exc:
            return err(400, str(exc))
    # 走券商：取真实成交 + 真实 K 线
    from tools import get_bridge
    broker_id = body.get("broker_id") or None
    period = body.get("period") or "1d"
    count = int(body.get("count") or 120)
    try:
        b = get_bridge(broker_id)
        if b is None:
            return err(503, "未连接券商客户端")
        deals = await b.call(b.gateway.get_deals)
        if not deals:
            return err(400, "无成交记录可归因")
        klbs: Dict[str, list] = {}
        for d in deals:
            code = (d.get("code") or "").upper()
            if code and code not in klbs:
                kr = await FR.fetch_kline_cached(code, period, count, broker_id=broker_id)
                klbs[code] = kr.get("bars") or []
        conv = [{"code": (d.get("code") or "").upper(),
                 "side": "sell" if str(d.get("direction", "")).lower().startswith("s") else "buy",
                 "price": float(d.get("price") or 0),
                 "qty": float(d.get("volume") or 0),
                 "time": d.get("time"),
                 "pnl": float(d.get("profit") or d.get("pnl") or 0)} for d in deals]
        return ok(attribute_pnl(conv, klbs))
    except BrokerError as exc:
        return err(503, str(exc))
