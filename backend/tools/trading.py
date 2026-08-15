"""交易类工具：place_order / cancel_order / cancel_order_price / query_position / query_cash / query_orders / query_deals。

下单前过统一风控（单笔金额/最小数量/单票上限/频率限制）；全部走真实券商 SDK；
所有交易动作写入审计日志（audit_log），工业级可追溯。
"""
import time

from . import get_bridge


def _audit(action: str, target: str, params: dict, result: str):
    from app.state import state
    if state.db is not None:
        try:
            state.db.audit("trading", action, target, params, result)
        except Exception:  # noqa: BLE001
            pass


# 幂等防重：idempotency_key -> (ts, result)，窗口 30s
_IDEMPOTENT_WINDOW = 30.0
_IDEMPOTENCY: dict[str, tuple[float, dict]] = {}


def _idempotent_get(key: str):
    if not key:
        return None
    hit = _IDEMPOTENCY.get(key)
    if hit and time.time() - hit[0] <= _IDEMPOTENT_WINDOW:
        return hit[1]
    if hit:
        _IDEMPOTENCY.pop(key, None)
    return None


def _idempotent_set(key: str, result: dict) -> None:
    if key:
        _IDEMPOTENCY[key] = (time.time(), result)


def register_trading_tools(mcp, risk):
    @mcp.tool()
    async def place_order(
        code: str,
        direction: str,
        volume: int,
        price: float = 0.0,
        price_type: str = "limit",   # limit | market
        strategy_name: str = "",
        remark: str = "",
        broker_id: str = "",
        idempotency_key: str = "",
    ) -> dict:
        """下单（限价/市价）。direction: buy/sell。下单前过统一风控。

        idempotency_key：可选幂等键，30s 内同键直接返回首次结果（防重复提交/网络重试双单）。
        """
        dup = _idempotent_get(idempotency_key)
        if dup is not None:
            dup["duplicated"] = True
            return dup
        b = get_bridge(broker_id or None)
        ok, reason = risk.check_order(code, price if price > 0 else 100.0, volume, direction)
        params = {"code": code, "direction": direction, "volume": volume,
                  "price": price, "price_type": price_type,
                  "strategy": strategy_name, "remark": remark, "broker_id": broker_id}
        if not ok:
            _audit("order.rejected", code, params, reason)
            return {"ok": False, "reason": reason}
        result = await b.call_locked(
            b.gateway.place_order, code, direction, price_type, price, volume,
            strategy_name, remark)
        result["ok"] = True
        _audit("order.submitted", code, params, f"order_id={result.get('order_id')}")
        _idempotent_set(idempotency_key, result)
        return result

    @mcp.tool()
    async def cancel_order(order_id: str, broker_id: str = "") -> dict:
        """撤单。"""
        b = get_bridge(broker_id or None)
        result = await b.call_locked(b.gateway.cancel_order, order_id)
        _audit("order.cancel", order_id, {"broker_id": broker_id}, "ok")
        return result

    @mcp.tool()
    async def cancel_order_price(order_id: str, deviation: float = 0.01,
                                 broker_id: str = "") -> dict:
        """超价撤单（偏离最新价超过 deviation 撤）。"""
        b = get_bridge(broker_id or None)
        result = await b.call_locked(b.gateway.cancel_order_price, order_id, deviation)
        _audit("order.cancel_price", order_id,
               {"deviation": deviation, "broker_id": broker_id}, "ok")
        return result

    @mcp.tool()
    async def query_position(broker_id: str = "", symbol: str = "") -> list[dict]:
        """查询当前持仓（可按代码过滤）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_positions, symbol or None)

    @mcp.tool()
    async def query_cash(broker_id: str = "") -> dict:
        """查询资金与资产。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_cash)

    @mcp.tool()
    async def query_orders(broker_id: str = "") -> list[dict]:
        """查询当日委托。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_orders)

    @mcp.tool()
    async def query_deals(broker_id: str = "") -> list[dict]:
        """查询当日成交。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_deals)
