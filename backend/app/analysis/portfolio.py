"""G10-2 组合层：持仓聚合 / 行业暴露 / 风险贡献。

输入：持仓列表（code/name/sector/qty/price/cost）+ 可选每标的历史 K 线
（用于风险贡献：以日收益波动率估计，组合风险 = 权重×波动率归一化）。
铁律：缺字段置 None/0，绝不估算（零 mock）；无 K 线时不产出风险贡献（null）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.indicators import builtin


@dataclass
class Position:
    """持仓项。price=现价，cost=成本价，qty=数量（股）。"""

    code: str
    name: str = ""
    sector: str = ""
    qty: float = 0.0
    price: float = 0.0
    cost: float = 0.0


def _vol(bars) -> Optional[float]:
    """日收益标准差（日级）；数据不足 → None。兼容 Bar 模型与 dict 两种输入。"""
    if not bars or len(bars) < 8:
        return None
    closes = []
    for b in bars:
        v = getattr(b, "close", None) if not isinstance(b, dict) else b.get("close")
        closes.append(float(v))
    r = builtin.returns(closes)
    vals = [x for x in r[1:] if x == x]           # 去 NaN
    if len(vals) < 8:
        return None
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var) if var > 0 else None


def aggregate(positions: List[Position],
              bars_by_code: Optional[Dict[str, list]] = None) -> Dict[str, Any]:
    """持仓聚合：市值/盈亏/权重/行业暴露/风险贡献。"""
    items = []
    total_mv = 0.0
    total_cost = 0.0
    sector_mv: Dict[str, float] = {}
    for p in positions:
        mv = p.qty * p.price
        cost = p.qty * p.cost
        total_mv += mv
        total_cost += cost
        sector_mv[p.sector or "未分类"] = sector_mv.get(p.sector or "未分类", 0.0) + mv
        items.append({
            "code": p.code, "name": p.name, "sector": p.sector or "未分类",
            "qty": p.qty, "price": p.price, "cost": p.cost,
            "market_value": round(mv, 2),
            "pnl": round(mv - cost, 2),
            "pnl_pct": round((mv - cost) / cost * 100.0, 2) if cost else None,
        })
    if total_mv > 0:
        for it in items:
            it["weight"] = round(it["market_value"] / total_mv, 4)

    # 风险贡献（可选，需 K 线）：贡献度 = w_i * σ_i / Σ(w_j * σ_j)
    risk_items: Optional[List[dict]] = None
    if bars_by_code:
        risk_items = []
        vol = {p.code: _vol(bars_by_code.get(p.code) or []) for p in positions}
        weighted = [(p, vol[p.code]) for p in positions
                    if vol[p.code] is not None and p.qty * p.price > 0]
        denom = sum((p.qty * p.price) * v for p, v in weighted)
        if denom > 0:
            risk_items = [{
                "code": p.code, "name": p.name,
                "vol_daily": round(v * 100, 4),
                "contribution_pct": round((p.qty * p.price) * v / denom * 100.0, 2),
            } for p, v in weighted]

    sector_exposure = [{"sector": s, "market_value": round(mv, 2),
                        "weight": round(mv / total_mv, 4) if total_mv else None}
                       for s, mv in sorted(sector_mv.items(), key=lambda kv: -kv[1])]

    return {
        "total_market_value": round(total_mv, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_mv - total_cost, 2),
        "total_pnl_pct": round((total_mv - total_cost) / total_cost * 100.0, 2) if total_cost else None,
        "position_count": len(items),
        "items": items,
        "sector_exposure": sector_exposure,
        "risk_contribution": risk_items,
    }


__all__ = ["Position", "aggregate", "_vol"]
