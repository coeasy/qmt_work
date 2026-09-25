"""组合级回测引擎（P1-3）：多标的 × 目标权重 × 共享现金 × 定期再平衡。

它补的是 ``tools.factor_backtest.run_portfolio_backtest`` 没覆盖的那一半
（**不是替代**，那个函数有 3 条回归测试守着的既有契约，继续保留）。

两者语义的差别
--------------
旧实现（sleeve 模式）::

    每标的一条**独立资金袖套**，最后按权重把净值加总。
    - 权重只在 t=0 成立：某标的清仓后它的现金**闲置**，不会被别的标的用；
    - 净值按**索引**相加 —— 各标的日期范围不一致时会把不同日期混在一起，
      得到一条在真实日历上**不存在**的组合净值曲线。

本模块（共享现金模式）::

    一个账户、多只持仓、同一本现金。
    - 组合净值 = 现金 + Σ 持仓市值，权重是**被维持**的目标（再平衡拉回偏离）；
    - 按交易日历**交集**对齐，日期不一致时**显式报告丢弃了多少根**，
      绝不 forward-fill（把「没有数据」假装成「净值不变」是假结论的温床）。

设计上刻意遵守的两条项目纪律
----------------------------
1. **「数据还没到」不得给确定性断言**：对齐后的缺口只报告，不填充；无法解析日期
   而用户又要求 ``weekly``/``monthly`` 再平衡时**直接报错**（不能静默降级成每日）。
2. **「查询失败 ≠ 没有数据」**：无成交时输出 ``diagnostics`` 区分「没信号」与
   「本金买不起一手」，避免把「钱不够」误读成「策略不赚钱」。

零 mock：所有价格来自调用方传入的真实 K 线。
"""
from __future__ import annotations

import math
from datetime import date as _date
from typing import Dict, List, Optional, Sequence, Tuple

from .ashare import is_limit_down, is_limit_up, round_lot
from .indicators import signals_for
from .metrics import compute_metrics

DEFAULT_MIN_LOT = 100

#: 支持的再平衡口径。``none`` = 只在首根建仓，之后既不调仓也不响应信号。
REBALANCE_MODES: Tuple[str, ...] = ("none", "daily", "weekly", "monthly", "signal")

#: 日期字段候选（券商 / 冷仓 / CSV 三种来源的键名不同）
_DATE_KEYS = ("date", "datetime", "time", "dt", "trading_date")


# ============================================================================
# 基础工具
# ============================================================================
def _num(v) -> Optional[float]:
    """宽松数值转换：转不出来或 NaN 一律 None（**不要**退化成 0）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _date_key(v) -> str:
    """把任意来源的日期值归一成**时间轴键**。

    ★ 为什么不能简单 ``str(v)[:10]``：分钟线的 ``"2024-01-05 09:31:00"`` 会被截成
      ``"2024-01-05"``，一天内的 240 根 bar 全部塌缩成同一天、只剩最后一根 ——
      净值曲线凭空少掉 239 个点，而**没有任何地方会报错**。所以带时间的值保留到分钟。
    """
    s = str(v).strip()
    if not s:
        return ""
    if "T" in s or ":" in s:
        base = s.replace("T", " ")
        head = base[:10]
        tail = base[11:16] if len(base) >= 16 else ""
        return (head + " " + tail).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if len(s) > 10:
        return s[:10]
    return s


def bar_date(bar: dict) -> str:
    """取 bar 的时间轴键；取不到返回空串。"""
    for k in _DATE_KEYS:
        v = bar.get(k)
        if v not in (None, ""):
            return _date_key(v)
    return ""


def _parse_date(s: str) -> Optional[_date]:
    """解析 ``YYYY-MM-DD`` / ``YYYYMMDD``；失败返回 None（不猜）。"""
    t = (s or "").strip()
    if not t:
        return None
    digits = t.replace("-", "").replace("/", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        try:
            return _date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _normalize_bar(bar: dict) -> Optional[dict]:
    """把任意来源的 bar 归一成引擎内部形态。

    丢弃规则（**必须报告，不得静默**）：
      - 无日期 → 无法放进时间轴；
      - close 缺失 / 非正 → 无法估值。
    """
    d = bar_date(bar)
    c = _num(bar.get("close"))
    if not d or c is None or c <= 0:
        return None
    return {
        "date": d,
        "open": _num(bar.get("open")),
        "high": _num(bar.get("high")),
        "low": _num(bar.get("low")),
        "close": c,
        "volume": _num(bar.get("volume")),
        "amount": _num(bar.get("amount")),
    }


def align_panel(klines: Dict[str, list], *, min_overlap: int = 30) -> dict:
    """把多标的 K 线按**交易日交集**对齐成一张面板。

    返回::

        {"symbols": [...], "dates": [...], "bars": {sym: [bar, ...]},
         "alignment": {"n_common_dates": n, "per_symbol": {sym: {...}}}}

    为什么是交集而不是并集 + 填充：并集必然要对缺失日填值，而**任何填充值都是编造的**。
    本项目已有多处教训（把「查不到」当成「没有」）。宁可少几根 bar 并说清楚，
    也不要在曲线上留下看不见的假点。
    """
    syms = [s for s in (klines or {}) if klines.get(s)]
    if not syms:
        raise ValueError("align_panel 至少需要 1 个有数据的标的")

    per_symbol: Dict[str, dict] = {}
    date_sets: List[set] = []
    by_date: Dict[str, Dict[str, dict]] = {}
    for sym in syms:
        raw = klines.get(sym) or []
        buckets: Dict[str, dict] = {}
        n_raw = 0
        n_bad = 0
        for b in raw:
            n_raw += 1
            nb = _normalize_bar(b) if isinstance(b, dict) else None
            if nb is None:
                n_bad += 1
                continue
            # 同一天多根（分钟线混入）时保留最后一根：日线口径下同一天只能有一个收盘
            buckets[nb["date"]] = nb
        by_date[sym] = buckets
        ds = set(buckets)
        date_sets.append(ds)
        # 「同键多根」= 归档里同一 code/period/dt 出现多次（或分钟线被压成同一天）。
        # 保留最后一根是合理策略，但**必须报出来**，否则用户无从知道少了多少根。
        per_symbol[sym] = {"n_raw": n_raw, "n_dropped_unusable": n_bad,
                           "n_unique_dates": len(ds),
                           "n_collapsed_same_key": max(0, n_raw - n_bad - len(ds))}

    common = set.intersection(*date_sets) if date_sets else set()
    dates = sorted(common)
    if len(dates) < min_overlap:
        raise ValueError(
            f"多标的共同交易日仅 {len(dates)} 天（需≥{min_overlap} 天）——"
            "各标的区间几乎不重叠，无法构成组合回测。请统一取数区间/根数。")

    for i, sym in enumerate(syms):
        ds = date_sets[i]
        info = per_symbol[sym]
        info["n_common"] = len(dates)
        info["n_excluded_nonoverlap"] = len(ds) - len(dates)
        # 只报「被截掉」的首末日期，便于用户自己判断区间差异
        excl = sorted(ds - common)
        info["excluded_range"] = [excl[0], excl[-1]] if excl else None

    return {
        "symbols": syms,
        "dates": dates,
        "bars": {sym: [by_date[sym][d] for d in dates] for sym in syms},
        "alignment": {"n_common_dates": len(dates), "per_symbol": per_symbol},
    }


def _require_all_present(requested: Sequence[str], available: Sequence[str]) -> None:
    """对齐后面板必须覆盖**全部**请求的标的。

    ★ 少一个就报错而不是「用剩下的继续跑」：静默少一个标的会让用户以为
    自己跑的是 5 只组合，实际是 4 只 —— 权重、净值、归因全错。
    """
    missing = [s for s in requested if s not in set(available)]
    if missing:
        raise ValueError(
            f"以下标的没有任何可用 K 线，无法参与组合：{missing}。"
            "请先补齐数据或从组合中移除。")


def normalize_weights(weights: Optional[Sequence[float]], n: int) -> List[float]:
    """归一化目标权重：缺省等权；总和 ≤0 时回退等权；超配（Σ>1）直接报错。

    ★ Σ>1 是**用户意图矛盾**（想买超过 100% 的仓位），报错而不是悄悄缩放到 1：
    悄悄缩放会让用户以为「我设的 1.5 倍杠杆生效了」。
    """
    if n <= 0:
        raise ValueError("标的数量必须为正")
    if weights is None:
        return [1.0 / n] * n
    # ★ 类型检查必须在「空判断」之前：`not 0.0` 为真，会把 weights=0.0 静默当成等权。
    if isinstance(weights, (str, bytes)) or not hasattr(weights, "__iter__"):
        raise ValueError(f"weights 必须是数值序列，收到 {type(weights).__name__}；"
                         "（提示：前 6 个位置参数是 symbols, klines, weights, strategy, params, capital）")
    ws = [float(w) for w in weights]
    if not ws:
        return [1.0 / n] * n
    if len(ws) != n:
        raise ValueError(f"权重数量({len(ws)})与标的数量({n})不一致")
    if any(w < 0 for w in ws):
        raise ValueError("权重不能为负（本引擎不支持做空）")
    s = sum(ws)
    if s <= 0:
        return [1.0 / n] * n
    if s > 1.0 + 1e-9:
        raise ValueError(f"权重合计 {s:.4f} > 1，本引擎不使用杠杆；请检查权重配置")
    # 允许 Σ<1（余下部分自动为现金），此时**不**归一化到 1
    return ws


def _bucket(d: str, mode: str) -> str:
    """再平衡周期桶。``weekly``/``monthly`` 需要可解析日期，否则报错（不静默降级）。"""
    if mode == "monthly":
        dt = _parse_date(d)
        if dt is None:
            raise ValueError(f"日期 {d!r} 无法解析，monthly 再平衡需要 YYYY-MM-DD/YYYYMMDD 格式")
        return f"{dt.year:04d}-{dt.month:02d}"
    if mode == "weekly":
        dt = _parse_date(d)
        if dt is None:
            raise ValueError(f"日期 {d!r} 无法解析，weekly 再平衡需要 YYYY-MM-DD/YYYYMMDD 格式")
        iso = dt.isocalendar()
        return f"{iso[0]:04d}W{iso[1]:02d}"
    return d


def _slip_rate(bar: dict, price: float, slippage_bps: float, use_spread: bool) -> float:
    """单边滑点比例。use_spread 时取 max(bps, 半价差/价格)。"""
    rate = float(slippage_bps) / 10000.0
    if not use_spread:
        return rate
    hi, lo = bar.get("high"), bar.get("low")
    if hi and lo and price > 0:
        half = (float(hi) - float(lo)) / 2.0 / price
        return max(rate, max(0.0, half))
    return rate


# ============================================================================
# 组合引擎
# ============================================================================
def run_portfolio_engine(
    symbols: Sequence[str],
    klines: Dict[str, list],
    weights: Optional[Sequence[float]],
    strategy: str,
    params: dict,
    initial_capital: float = 1_000_000.0,
    *,
    commission_rate: float = 0.0003,
    stamp_tax: float = 0.001,
    slippage_bps: float = 5.0,
    min_lot: int = DEFAULT_MIN_LOT,
    lot_rounding: bool = True,
    rebalance: str = "signal",
    enforce_limit: bool = True,
    cash_buffer: float = 0.0,
    use_spread_slippage: bool = False,
    max_participation_pct: float = 1.0,
    is_st: bool = False,
    period: str = "1d",
    rf: float = 0.0,
    liquidate_at_end: bool = False,
    min_overlap: int = 30,
) -> dict:
    """多标的共享现金组合回测。

    ★ 前 6 个位置参数顺序**刻意**与 ``factor_backtest.run_portfolio_backtest`` 一致
      （``symbols, klines, weights, strategy, params, initial_capital``）：
      两个函数会并排使用，参数顺序不同必然导致「从另一个函数抄过来」的静默错参。

    参数
    ----
    rebalance      : ``none`` / ``daily`` / ``weekly`` / ``monthly`` / ``signal``（见 REBALANCE_MODES）
    cash_buffer    : 常备现金比例（0~1），目标仓位只用到 ``1-cash_buffer`` 的净值
    liquidate_at_end: 末根是否强制清仓。**默认 False** —— 组合净值已含持仓市值，
                     强平会把未实现盈亏一次性转为已实现并多记一笔成本，污染换手统计。
                     需要与 sleeve 模式（那个会强平）逐项对比时再打开。
    max_participation_pct: 单根成交量参与率上限（1.0 = 不限）

    返回
    ----
    equity_curve / metrics / turnover / cost / attribution / final_positions /
    final_cash / alignment / rebalance_log / diagnostics
    """
    syms = [str(s) for s in (symbols or [])]
    if not syms:
        raise ValueError("至少需 1 个标的")
    if rebalance not in REBALANCE_MODES:
        raise ValueError(f"未知 rebalance={rebalance!r}，可选 {REBALANCE_MODES}")
    if not 0.0 <= cash_buffer < 1.0:
        raise ValueError("cash_buffer 必须在 [0, 1) 区间")

    panel = align_panel({s: klines.get(s) for s in syms}, min_overlap=min_overlap)
    _require_all_present(syms, panel["symbols"])
    dates: List[str] = panel["dates"]
    bars_by_sym: Dict[str, List[dict]] = panel["bars"]
    T = len(dates)
    w = normalize_weights(weights, len(syms))

    # 信号：逐标的在**对齐后**的收盘序列上生成（先对齐再算信号，
    # 否则信号索引与面板索引对不上 —— 这正是旧实现按索引相加的同一个坑）。
    closes: Dict[str, List[float]] = {}
    sigs: Dict[str, List[int]] = {}
    for s in syms:
        cs = [b["close"] for b in bars_by_sym[s]]
        closes[s] = cs
        sigs[s] = [int(x) for x in list(signals_for(strategy, cs, params or {}))]

    shares: Dict[str, int] = {s: 0 for s in syms}
    cost_basis: Dict[str, float] = {s: 0.0 for s in syms}
    realized: Dict[str, float] = {s: 0.0 for s in syms}
    sym_trades: Dict[str, int] = {s: 0 for s in syms}
    sym_turnover: Dict[str, float] = {s: 0.0 for s in syms}
    sym_cost: Dict[str, float] = {s: 0.0 for s in syms}
    blocked_buy: Dict[str, int] = {s: 0 for s in syms}
    blocked_sell: Dict[str, int] = {s: 0 for s in syms}

    cash = float(initial_capital)
    equity: List[float] = []
    trades: List[dict] = []
    rebalance_log: List[dict] = []
    totals = {"commission": 0.0, "stamp_tax": 0.0, "slippage": 0.0, "turnover": 0.0}

    def _mark(t: int) -> float:
        return cash + sum(shares[s] * closes[s][t] for s in syms)

    for t in range(T):
        # ---- 1. 决定本根是否再平衡 ----
        if t == 0:
            do_reb = True
        elif rebalance == "daily":
            do_reb = True
        elif rebalance == "signal":
            do_reb = any(sigs[s][t] != sigs[s][t - 1] for s in syms)
        elif rebalance in ("weekly", "monthly"):
            do_reb = _bucket(dates[t], rebalance) != _bucket(dates[t - 1], rebalance)
        else:  # "none"
            do_reb = False

        if do_reb:
            equity_now = _mark(t)
            investable = equity_now * (1.0 - cash_buffer)
            target_shares: Dict[str, int] = {}
            for i, s in enumerate(syms):
                px = closes[s][t]
                if not sigs[s][t] or px <= 0:
                    target_shares[s] = 0
                    continue
                tgt_val = investable * w[i]
                raw_q = tgt_val / px
                target_shares[s] = (round_lot(raw_q, min_lot) if lot_rounding
                                    else int(raw_q))
            sold = bought = 0
            # ---- 2a. 先卖（腾出现金），跌停卖不出则跳过 ----
            for s in syms:
                gap = shares[s] - target_shares[s]
                if gap <= 0:
                    continue
                bar, px = bars_by_sym[s][t], closes[s][t]
                ref = closes[s][t - 1] if t > 0 else px
                if enforce_limit and is_limit_down(s, px, ref, is_st):
                    blocked_sell[s] += 1
                    continue
                qty = gap
                if max_participation_pct < 1.0 and bar.get("volume"):
                    cap = int(float(bar["volume"]) * max_participation_pct)
                    qty = min(qty, round_lot(cap, min_lot) if lot_rounding else cap)
                if qty <= 0:
                    continue
                rate = _slip_rate(bar, px, slippage_bps, use_spread_slippage)
                px_ex = px * (1.0 - rate)
                gross = qty * px_ex
                comm = gross * commission_rate
                stamp = gross * stamp_tax
                net = gross - comm - stamp
                pnl = net - cost_basis[s] * (qty / shares[s])
                cost_basis[s] *= (1.0 - qty / shares[s])
                cash += net
                shares[s] -= qty
                realized[s] += pnl
                sym_trades[s] += 1
                sym_turnover[s] += qty * px
                sym_cost[s] += comm + stamp + qty * px * rate
                totals["commission"] += comm
                totals["stamp_tax"] += stamp
                totals["slippage"] += qty * px * rate
                totals["turnover"] += qty * px
                sold += 1
                trades.append({"time": dates[t], "date": dates[t], "code": s, "side": "sell",
                               "price": round(px_ex, 4), "qty": int(qty),
                               "pnl": round(pnl, 2), "commission": round(comm, 2),
                               "stamp_tax": round(stamp, 2), "reason": "rebalance"})
            # ---- 2b. 再买（受可用现金约束），涨停买不进则跳过 ----
            buy_list = sorted((s for s in syms if target_shares[s] > shares[s]),
                              key=lambda s: -(target_shares[s] - shares[s]) * closes[s][t])
            for s in buy_list:
                bar, px = bars_by_sym[s][t], closes[s][t]
                ref = closes[s][t - 1] if t > 0 else px
                if enforce_limit and is_limit_up(s, px, ref, is_st):
                    blocked_buy[s] += 1
                    continue
                rate = _slip_rate(bar, px, slippage_bps, use_spread_slippage)
                px_ex = px * (1.0 + rate)
                unit = px_ex * (1.0 + commission_rate)
                affordable = (round_lot(cash / unit, min_lot) if lot_rounding
                              else int(cash / unit))
                qty = min(target_shares[s] - shares[s], affordable)
                if max_participation_pct < 1.0 and bar.get("volume"):
                    cap = int(float(bar["volume"]) * max_participation_pct)
                    qty = min(qty, round_lot(cap, min_lot) if lot_rounding else cap)
                if qty <= 0:
                    continue
                gross = qty * px_ex
                comm = gross * commission_rate
                cash -= gross + comm
                shares[s] += qty
                cost_basis[s] += gross + comm
                sym_trades[s] += 1
                sym_turnover[s] += qty * px
                sym_cost[s] += comm + qty * px * rate
                totals["commission"] += comm
                totals["slippage"] += qty * px * rate
                totals["turnover"] += qty * px
                bought += 1
                trades.append({"time": dates[t], "date": dates[t], "code": s, "side": "buy",
                               "price": round(px_ex, 4), "qty": int(qty),
                               "cost": round(gross + comm, 2), "commission": round(comm, 2),
                               "reason": "rebalance"})
            if sold or bought:
                rebalance_log.append({"date": dates[t], "sold": sold, "bought": bought,
                                      "equity": round(equity_now, 2)})

        equity.append(_mark(t))

    # ---- 3. 可选末根清仓（默认不做，见 docstring）----
    if liquidate_at_end and any(shares[s] > 0 for s in syms):
        t = T - 1
        for s in syms:
            if shares[s] <= 0:
                continue
            px = closes[s][t]
            rate = _slip_rate(bars_by_sym[s][t], px, slippage_bps, use_spread_slippage)
            px_ex = px * (1.0 - rate)
            gross = shares[s] * px_ex
            comm = gross * commission_rate
            stamp = gross * stamp_tax
            net = gross - comm - stamp
            pnl = net - cost_basis[s]
            cash += net
            realized[s] += pnl
            sym_trades[s] += 1
            sym_turnover[s] += shares[s] * px
            sym_cost[s] += comm + stamp + shares[s] * px * rate
            totals["commission"] += comm
            totals["stamp_tax"] += stamp
            totals["slippage"] += shares[s] * px * rate
            totals["turnover"] += shares[s] * px
            trades.append({"time": dates[t], "date": dates[t], "code": s, "side": "sell",
                           "price": round(px_ex, 4), "qty": int(shares[s]),
                           "pnl": round(pnl, 2), "commission": round(comm, 2),
                           "stamp_tax": round(stamp, 2), "reason": "liquidate"})
            shares[s] = 0
            cost_basis[s] = 0.0
        equity[-1] = cash

    metrics = compute_metrics(equity, trades, period=period, rf=rf, dates=dates)

    # ---- 4. 归因：分标的的已实现 / 未实现 / 成本 / 换手 ----
    attribution: Dict[str, dict] = {}
    for s in syms:
        unreal = shares[s] * closes[s][T - 1] - cost_basis[s]
        attribution[s] = {
            "realized_pnl": round(realized[s], 2),
            "unrealized_pnl": round(unreal, 2),
            "total_pnl": round(realized[s] + unreal, 2),
            "cost": round(sym_cost[s], 2),
            "turnover": round(sym_turnover[s], 2),
            "trades": sym_trades[s],
            "final_shares": int(shares[s]),
            "weight_target": round(w[syms.index(s)], 4),
        }

    out = {
        "symbols": syms,
        "weights": [round(x, 6) for x in w],
        "strategy": strategy,
        "params": params or {},
        "initial_capital": float(initial_capital),
        "rebalance": rebalance,
        "cash_buffer": cash_buffer,
        "engine": "portfolio_shared_cash",
        "period": period,
        "n_bars": T,
        "dates": dates[-200:],
        "equity_curve": [round(e, 2) for e in equity[-200:]],
        "final_equity": round(equity[-1], 2) if equity else float(initial_capital),
        "metrics": metrics,
        "turnover": {
            "total_notional": round(totals["turnover"], 2),
            "n_rebalances": len(rebalance_log),
            "turnover_pct_of_capital": (round(totals["turnover"] / initial_capital, 4)
                                        if initial_capital else None),
        },
        "cost": {
            "commission": round(totals["commission"], 2),
            "stamp_tax": round(totals["stamp_tax"], 2),
            "slippage": round(totals["slippage"], 2),
            "total": round(totals["commission"] + totals["stamp_tax"] + totals["slippage"], 2),
            "cost_pct_of_capital": (round(
                (totals["commission"] + totals["stamp_tax"] + totals["slippage"])
                / initial_capital, 4) if initial_capital else None),
            "rates": {"commission_rate": commission_rate, "stamp_tax": stamp_tax,
                      "slippage_bps": slippage_bps},
        },
        "attribution": attribution,
        "final_positions": {s: int(shares[s]) for s in syms},
        "final_cash": round(cash, 2),
        "blocked": {"buy_limit_up": {s: blocked_buy[s] for s in syms if blocked_buy[s]},
                    "sell_limit_down": {s: blocked_sell[s] for s in syms if blocked_sell[s]}},
        "rebalance_log": rebalance_log[-20:],
        "trades": trades[-20:],
        "trade_count": len(trades),
        "alignment": panel["alignment"],
        "source": "real_kline",
    }
    _annotate_diagnostics(out, sigs, closes, min_lot, initial_capital, w, syms)
    return out


def _annotate_diagnostics(out: dict, sigs: Dict[str, List[int]], closes: Dict[str, List[float]],
                          min_lot: int, initial_capital: float, w: List[float],
                          syms: List[str]) -> None:
    """0 成交时必须说清成因 —— 「没信号」和「买不起一手」是完全不同的两件事。"""
    if out["trade_count"] > 0:
        return
    ever_long = [s for s in syms if any(sigs[s])]
    if not ever_long:
        out["diagnostics"] = "策略在该区间未产生任何持仓信号（可调整参数或拉长区间）"
        return
    # 找出「最便宜的一手」与它分到的资金，判断是不是钱不够
    detail = []
    for i, s in enumerate(syms):
        if not any(sigs[s]):
            continue
        hold_px = [closes[s][j] for j in range(len(closes[s])) if sigs[s][j]]
        if not hold_px:
            continue
        need = min(hold_px) * min_lot
        have = initial_capital * w[i]
        detail.append((s, min(hold_px), need, have))
    poor = [d for d in detail if d[2] > d[3]]
    if detail and len(poor) == len(detail):
        s, mp, need, have = min(detail, key=lambda d: d[3] - d[2])
        out["diagnostics"] = (
            f"产生了持仓信号但无成交：初始资金不足。{s} 持仓信号期间最低价 {mp:.2f} 元，"
            f"买入 1 手（{min_lot} 股）约需 {need:,.0f} 元，而其分配到的资金仅 {have:,.0f} 元。")
    else:
        out["diagnostics"] = (
            "产生了持仓信号但无成交：可能被「涨停买不进 / 跌停卖不出」或成交量参与率约束拦截；"
            "可尝试放宽 max_participation_pct 或关闭 enforce_limit 复测。")


# ============================================================================
# 本地数据装载（P1-3 验收：先用 bars_cold.db / CSV 真实历史验证，不依赖真券商）
# ============================================================================
def load_bars_from_cold_db(db_path, symbols: Optional[Sequence[str]] = None,
                           period: str = "1d", limit_per_symbol: int = 0) -> Dict[str, list]:
    """从冷仓 SQLite（``kline_archive`` 表）读取历史 K 线，只读、不联网。

    表结构：``code, period, dt, open, high, low, close, volume, amount, adjust``。
    ``dt`` 形如 ``20240409``。``limit_per_symbol>0`` 时每只只取最近 N 根。
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = ("SELECT code, dt, open, high, low, close, volume, amount "
               "FROM kline_archive WHERE period=?")
        args: list = [period]
        if symbols:
            marks = ",".join("?" * len(symbols))
            sql += f" AND code IN ({marks})"
            args.extend(list(symbols))
        sql += " ORDER BY code, dt"
        out: Dict[str, list] = {}
        for code, dt, o, h, lo, c, v, amt in conn.execute(sql, args):
            out.setdefault(code, []).append(
                {"time": str(dt), "open": o, "high": h, "low": lo, "close": c,
                 "volume": v, "amount": amt})
        if limit_per_symbol > 0:
            out = {k: v[-limit_per_symbol:] for k, v in out.items()}
        return out
    finally:
        conn.close()


def load_bars_from_csv_dir(dir_path, symbols: Optional[Sequence[str]] = None,
                           suffix: str = "_1d.csv") -> Dict[str, list]:
    """从 ``kline_data/all_a_share`` 这类 CSV 目录读真实历史（表头 ``time,open,high,low,close,volume,amount``）。"""
    import csv
    import os

    out: Dict[str, list] = {}
    want = set(symbols) if symbols else None
    for name in sorted(os.listdir(dir_path)):
        if not name.endswith(suffix):
            continue
        code = name[: -len(suffix)]
        if want and code not in want:
            continue
        bars: list = []
        with open(os.path.join(dir_path, name), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                bars.append({"time": row.get("time"), "open": row.get("open"),
                             "high": row.get("high"), "low": row.get("low"),
                             "close": row.get("close"), "volume": row.get("volume"),
                             "amount": row.get("amount")})
        out[code] = bars
    return out
