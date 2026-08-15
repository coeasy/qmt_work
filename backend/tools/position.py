"""目标仓位下单（借鉴 Rockyzsu/QMT 目标仓位能力，真实账户数据）。

- 输入：code / target_pct（目标市值占总资产比例）
- 计算：目标市值 = 总资产 × target_pct；对比当前持仓市值 → 差额 → 折股数（100 股整）
- do_trade=True 时经统一风控下单；否则仅返回调仓计划
"""
from . import get_bridge
from xtquant_client.base import BrokerError


def _audit(action: str, target: str, params: dict, result: str):
    from app.state import state
    if state.db is not None:
        try:
            state.db.audit("position", action, target, params, result)
        except Exception:  # noqa: BLE001
            pass


def register_position_tools(mcp, risk):
    @mcp.tool()
    async def order_target_position(code: str, target_pct: float,
                                    price: float = 0.0, do_trade: bool = False,
                                    broker_id: str = "") -> dict:
        """目标仓位调仓：把 code 的持仓市值调整到占总资产的 target_pct（0~1）。

        do_trade=True 时实际下单（过风控），否则仅返回调仓计划。
        """
        b = get_bridge(broker_id or None)
        cash = await b.call(b.gateway.get_cash)
        assets = float(cash.get("assets", 0) or 0)
        if assets <= 0:
            raise BrokerError("账户总资产为 0，无法计算目标仓位")
        target_pct = float(target_pct)
        if not 0 <= target_pct <= 1:
            raise BrokerError("target_pct 须在 0~1 之间")
        positions = await b.call(b.gateway.get_positions, code)
        cur_mv = sum(float(p.get("market_value", 0) or 0) for p in positions)
        target_mv = assets * target_pct
        diff = target_mv - cur_mv
        if abs(diff) < 500:
            return {"code": code, "target_pct": target_pct, "assets": assets,
                    "current_mv": cur_mv, "target_mv": target_mv, "diff": round(diff, 2),
                    "action": "none", "reason": "差额过小无需调仓"}
        quote_px = float(price or 0)
        if quote_px <= 0:
            q = await b.call(b.gateway.get_quote, code)
            quote_px = float(q.get("last") or 0)
        if quote_px <= 0:
            raise BrokerError("无法获取最新价，请显式传入 price")
        direction = "buy" if diff > 0 else "sell"
        volume = (int(abs(diff) / quote_px) // 100) * 100
        if volume <= 0:
            return {"code": code, "target_pct": target_pct, "assets": assets,
                    "current_mv": cur_mv, "target_mv": target_mv, "diff": round(diff, 2),
                    "action": "none", "reason": f"折股数不足 100 股（{abs(diff)/quote_px:.0f} 股）"}
        plan = {"code": code, "target_pct": target_pct, "assets": assets,
                "current_mv": cur_mv, "target_mv": target_mv, "diff": round(diff, 2),
                "direction": direction, "volume": volume,
                "price": round(quote_px, 4), "action": "trade"}
        if not do_trade:
            _audit("target_position.plan", code, {"target_pct": target_pct,
                                                  "direction": direction,
                                                  "volume": volume}, "plan_only")
            return plan
        ok, reason = risk.check_order(code, quote_px, volume, direction)
        if not ok:
            _audit("target_position.rejected", code, plan, reason)
            return {**plan, "ok": False, "reason": reason}
        res = await b.call_locked(b.gateway.place_order, code, direction,
                                  "limit", quote_px, volume, "target_position", "")
        _audit("target_position.order", code, plan, f"order_id={res.get('order_id')}")
        return {**plan, "ok": True, "order_id": res.get("order_id")}
