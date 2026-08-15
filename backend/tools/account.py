"""账户类工具：monitor_account / account_status（真实资金 + 持仓 + 连接状态）。"""
from . import get_bridge


def register_account_tools(mcp):
    @mcp.tool()
    async def monitor_account(broker_id: str = "") -> dict:
        """账户实时快照：资金 + 持仓 + 连接状态。"""
        b = get_bridge(broker_id or None)
        cash = await b.call(b.gateway.query_cash)
        pos = await b.call(b.gateway.query_position)
        return {"connected": b.gateway.is_connected(), "cash": cash, "positions": pos}

    @mcp.tool()
    async def account_status(broker_id: str = "") -> dict:
        """账户状态摘要（持仓市值、资产合计）。"""
        snap = await monitor_account(broker_id)
        pos_value = sum(p.get("market_value", 0.0) for p in snap["positions"])
        return {"assets": snap["cash"].get("assets", 0.0) + pos_value,
                "position_count": len(snap["positions"])}
