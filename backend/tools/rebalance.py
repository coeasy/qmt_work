"""分仓再平衡（覆盖 EzQmt Reblance.py：等权篮子 + 阈值调仓 + 拆单 + 涨跌停处理）。

generate_rebalance(targets, ...) 按目标市值占比计算调仓单：
- 等权目标：targets 给出每标的目标占比
- 阈值过滤：差异 < delta_min 不调；差异 > delta_max 拆为多笔（每笔约 delta_max）
- 涨跌停处理：买单价=涨停或卖单价=跌停时跳过（无法成交）
- 落库 rebalance_orders 并可经券商真实下单
"""
from . import get_bridge
from .limitup import _limit_factor


def _at_limit(quote: dict, direction: str) -> bool:
    """真实涨跌停判定：以昨收*(1±板块幅度) 计算涨停/跌停价，而非用当日最高/最低价近似。

    板块幅度按代码前缀：创业板/科创板 20%、北交所 30%、其余 10%（ST 标记由上层处理）。
    """
    last = quote.get("last")
    lc = quote.get("lastClose")
    if last is None or not lc:
        return False
    pct = _limit_factor(quote.get("code", ""))
    limit_up = round(lc * (1 + pct), 2)
    limit_down = round(lc * (1 - pct), 2)
    if direction == "buy" and last >= limit_up - 1e-9:
        return True
    if direction == "sell" and last <= limit_down + 1e-9:
        return True
    return False


def register_rebalance_tools(mcp):
    @mcp.tool()
    async def generate_rebalance(
        targets: list[dict],
        delta_min: float = 3000.0,
        delta_max: float = 30000.0,
        do_trade: bool = False,
        broker_id: str = "",
    ) -> dict:
        """等权篮子再平衡：targets=[{code,target_ratio}] -> 调仓单（阈值过滤+拆单+涨跌停处理）。

        do_trade=True 时经券商真实下单；否则仅生成计划。
        """
        if not targets:
            return {"ok": False, "reason": "targets 不能为空"}
        b = get_bridge(broker_id or None)
        cash = await b.call(b.gateway.get_cash)
        pos = await b.call(b.gateway.get_positions)
        total = cash.get("assets", 0.0) + sum(p.get("market_value", 0.0) for p in pos)
        current = {p["code"]: p.get("market_value", 0.0) for p in pos}
        orders = []
        for t in targets:
            code = t["code"]
            target_val = total * float(t.get("target_ratio", 0))
            diff = round(target_val - current.get(code, 0.0), 2)
            if abs(diff) < delta_min:
                continue
            quote = await b.call(b.gateway.get_quote, code)
            if _at_limit(quote, "buy" if diff > 0 else "sell"):
                orders.append({"code": code, "skipped": "limit", "diff": diff})
                continue
            price = quote["last"]
            if not price:
                continue
            remaining = abs(diff)
            while remaining > 1:
                lot = min(remaining, delta_max)
                volume = int(lot // price // 100 * 100)
                if volume <= 0:
                    break
                direction = "buy" if diff > 0 else "sell"
                if do_trade:
                    res = await b.call_locked(
                        b.gateway.place_order, code, direction, "limit", price, volume,
                        "rebalance", f"rebal-{code}")
                    orders.append({"code": code, "direction": direction,
                                   "volume": volume, "price": price, "order": res})
                else:
                    orders.append({"code": code, "direction": direction,
                                   "volume": volume, "price": price})
                remaining -= lot
        return {"ok": True, "total_assets": round(total, 2),
                "generated": len([o for o in orders if "direction" in o]), "orders": orders}
