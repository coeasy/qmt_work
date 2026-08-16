"""可参数化撮合内核（A 股规则感知）。

设计目标：回测（legacy / vectorized 双内核）与模拟盘共用同一撮合语义，消除
「功能级」与「A 股规则正确级」之间的漂移。所有逻辑为纯函数，不依赖任何
外部行情/交易 SDK，便于单测。

支持的参数化维度（见 `MatchingConfig`）：
- 执行时点 execution_timing：当根 close / 次根 open（避免用未来信息成交）
- 滑点模型：固定 bps / 价差模型（(high-low)/2）/ 成交量参与率上限（容量）
- 涨跌停不可成交：涨停买不进、跌停卖不出（enforce_limit）
- 成交量容量约束：单根成交量参与率上限（max_participation_pct）
- A 股规则：100 股整手、成本模型（佣金双边 / 印花税卖出）

`simulate()` 返回 (metrics, trades, equity)，形状与旧 `_simulate` 对齐，便于平滑替换。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .ashare import (
    is_limit_down, is_limit_up, round_lot,
)


@dataclass
class MatchingConfig:
    execution_timing: str = "close"          # "close" | "next_open"
    slippage_bps: float = 5.0                 # 固定滑点（单边 bp）
    use_spread_slippage: bool = False         # True 时滑点取 max(bps, (high-low)/2/price)
    max_participation_pct: float = 1.0        # 单根成交量参与率上限（1.0=不限）
    enforce_limit: bool = True                # 涨停买不进 / 跌停卖不出
    commission_rate: float = 0.0003           # 佣金（双边，默认万 3）
    stamp_tax: float = 0.001                  # 印花税（仅卖出，千 1）
    min_lot: int = 100                        # 整手
    lot_rounding: bool = True                 # 是否强制整手
    code: str = ""                            # 用于板块涨跌停幅度推导
    is_st: bool = False                       # ST 股 5% 幅度


def _exec_price(bar: dict, timing: str) -> float:
    """取成交参考价：close 用当根收盘，next_open 用次根开盘（缺省退回收盘）。"""
    if timing == "next_open":
        o = bar.get("open")
        if o not in (None, 0):
            return float(o)
    c = bar.get("close")
    return float(c) if c is not None else 0.0


def _exec_sig(sig: list[int], timing: str) -> list[int]:
    """执行时点对齐：next_open 下信号整体后移一根（第 0 根无前置信号→空仓）。"""
    if timing == "next_open":
        return [0] + sig[:-1]
    return list(sig)


def _ref_closes(closes: list[float]) -> list[float]:
    """昨收序列（首根用自身，避免越界；真实涨停判定从第二根起生效）。"""
    return [closes[0]] + closes[:-1]


def simulate(closes: list[float], sig: list[int], kline: list[dict],
             cfg: MatchingConfig, initial_capital: float) -> dict:
    """统一撮合内核。

    参数：
      closes: 收盘价序列
      sig:    与 closes 等长仓位信号（1=持有, 0=空仓）
      kline:  与 closes 等长的 K 线（需 open/high/low/close/volume；缺失字段降级处理）
      cfg:    MatchingConfig
      initial_capital: 初始资金

    返回：{metrics, trades, equity}
    """
    n = len(closes)
    if n < 2:
        return {"metrics": {}, "trades": [], "equity": [float(initial_capital)] * max(1, n)}

    slip = float(cfg.slippage_bps) / 10000.0
    exec_sig = _exec_sig(sig, cfg.execution_timing)
    ref = _ref_closes(closes)
    bars = kline if kline else [{} for _ in range(n)]

    cash = float(initial_capital)
    shares = 0
    cost_basis = 0.0          # 当前持仓总成本（含佣金），用于卖出盈亏
    equity: list[float] = []
    trades: list[dict] = []

    for i in range(n):
        price = closes[i]
        bar = bars[i] if i < len(bars) else {}
        vol = float(bar.get("volume") or 0)
        target = exec_sig[i]

        if target == 1:
            # ---- 持多：在容量约束下逐步建仓（涨停买不进则持有）----
            ep = _exec_price(bar, cfg.execution_timing)
            if ep <= 0:
                equity.append(cash + shares * price)
                continue
            if cfg.enforce_limit and is_limit_up(cfg.code, price, ref[i], cfg.is_st):
                equity.append(cash + shares * price)  # 涨停买不进
                continue
            buy_px = ep * (1 + slip)
            if cfg.use_spread_slippage:
                hi, lo = float(bar.get("high") or ep), float(bar.get("low") or ep)
                buy_px = max(buy_px, ep + (hi - lo) / 2.0 / max(ep, 1e-9))
            per = buy_px * (1 + cfg.commission_rate)
            desired = math.floor(cash / per) if per > 0 else 0
            if cfg.lot_rounding:
                desired = round_lot(desired, cfg.min_lot)
            to_buy = desired - shares          # 仍有空间才买（支持容量分笔建仓）
            if to_buy <= 0:
                equity.append(cash + shares * price)
                continue
            if cfg.max_participation_pct < 1.0 and vol > 0:
                cap = round_lot(int(vol * cfg.max_participation_pct), cfg.min_lot)
                to_buy = min(to_buy, cap)
            if to_buy <= 0:
                equity.append(cash + shares * price)
                continue
            cost = to_buy * per
            cash -= cost
            shares += to_buy
            cost_basis += cost
            trades.append({"time": bar.get("time"), "side": "buy",
                           "price": round(buy_px, 4), "qty": int(to_buy),
                           "cost": round(cost, 2)})

        elif target == 0 and shares > 0:
            # ---- 空仓：在容量约束下逐步平仓（跌停卖不出则持有）----
            ep = _exec_price(bar, cfg.execution_timing)
            if ep <= 0:
                equity.append(cash + shares * price)
                continue
            if cfg.enforce_limit and is_limit_down(cfg.code, price, ref[i], cfg.is_st):
                equity.append(cash + shares * price)  # 跌停卖不出
                continue
            sell_px = ep * (1 - slip)
            if cfg.use_spread_slippage:
                hi, lo = float(bar.get("high") or ep), float(bar.get("low") or ep)
                sell_px = min(sell_px, ep - (hi - lo) / 2.0 / max(ep, 1e-9))
            to_sell = shares
            if cfg.max_participation_pct < 1.0 and vol > 0:
                cap = round_lot(int(vol * cfg.max_participation_pct), cfg.min_lot)
                to_sell = min(shares, cap)
                if to_sell <= 0:
                    equity.append(cash + shares * price)
                    continue
            if cfg.lot_rounding:
                to_sell = round_lot(to_sell, cfg.min_lot)
            to_sell = min(to_sell, shares)
            if to_sell <= 0:
                equity.append(cash + shares * price)
                continue
            proceeds = to_sell * sell_px * (1 - cfg.commission_rate - cfg.stamp_tax)
            realized = proceeds - cost_basis * (to_sell / shares) if shares else 0.0
            cost_basis *= (1 - to_sell / shares)
            cash += proceeds
            shares -= to_sell
            trades.append({"time": bar.get("time"), "side": "sell",
                           "price": round(sell_px, 4), "qty": int(to_sell),
                           "pnl": round(realized, 2)})

        else:
            equity.append(cash + shares * price)

    # 末笔持仓按收盘价强制平仓（计入盈亏，与旧引擎语义一致）
    if shares > 0 and trades and trades[-1]["side"] == "buy":
        last = closes[-1]
        sell_px = last * (1 - slip)
        if cfg.use_spread_slippage:
            hi = float(bars[-1].get("high") or last); lo = float(bars[-1].get("low") or last)
            sell_px = min(sell_px, last - (hi - lo) / 2.0 / max(last, 1e-9))
        proceeds = shares * sell_px * (1 - cfg.commission_rate - cfg.stamp_tax)
        realized = proceeds - cost_basis
        trades.append({"time": bars[-1].get("time"), "side": "sell",
                       "price": round(sell_px, 4), "qty": int(shares),
                       "pnl": round(realized, 2)})
        cash += proceeds
        shares = 0
        equity[-1] = cash

    # 指标统一交给 tools.metrics.compute_metrics（年化按 bar 频率 / 可含基准），
    # 此处只返回净值与成交，避免两套指标逻辑漂移。
    return {"equity": equity, "trades": trades}
