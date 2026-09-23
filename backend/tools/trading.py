"""交易类工具：place_order / cancel_order / cancel_order_price / query_position / query_cash / query_orders / query_deals。

下单经 SignalRouter 统一链路（ExecutionMode/风控/幂等/WAL/审计）；撤单与查询走
ExecutionService / 真实券商 SDK；所有交易动作写入审计日志（audit_log），工业级可追溯。
"""
from core.state import state
from gateway.execution import ExecutionService

from . import get_bridge


def _audit(action: str, target: str, params: dict, result: str):
    from core.state import state
    if state.db is not None:
        try:
            state.db.audit("trading", action, target, params, result)
        except Exception:  # noqa: BLE001
            pass


def register_trading_tools(mcp, risk):
    execution = ExecutionService(risk=risk, db=state.db)
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
        auto_confirm: bool = False,
    ) -> dict:
        """下单（限价/市价）。direction: buy/sell。下单前过统一风控。

        V9 Execution Unification：MCP 下单经 SignalRouter 统一链路
        （ExecutionMode live/paper/dry_run + 风控 + 幂等 + WAL + 审计），
        与 REST/引擎单语义完全一致，不再拥有独立执行路径。

        idempotency_key：可选幂等键，窗口内同键直接返回首次结果（防重复提交/网络重试双单）。

        auto_confirm：是否跳过「大额 TOTP 二次确认挂起」。
        ★ 默认 **False**。此前这里写死 True —— MCP 是面向外部 Agent 的**开放入口**，
        写死 True 等于让任何拿到 admin key 的调用方都能绕过二次确认直接下大额单，
        仅靠 instructions 文本约束（文本不是权限）。默认关闭后，超额单会返回
        ``pending_confirmation`` + ``confirm_token``，由调用方显式确认后再成交。
        确需无人值守的自动化场景，请**显式**传 auto_confirm=True 并自行承担后果。
        """
        if state.signal_router is None:
            return {"ok": False, "reason": "统一信号入口未初始化"}
        return await state.signal_router.submit(
            code, direction, int(volume), float(price or 0), price_type,
            source=str(strategy_name or "mcp"),
            broker_id=str(broker_id or ""), remark=str(remark or ""),
            idempotency_key=str(idempotency_key or ""),
            auto_confirm=bool(auto_confirm))

    @mcp.tool()
    async def cancel_order(order_id: str, broker_id: str = "") -> dict:
        """撤单。"""
        # ★ 模拟盘委托号不得递给券商（R25 补齐）：与 /trade/cancel 同一道闸，
        #   否则适配器 int("PAPER-x") 抛 ValueError ⇒ 全局 500。
        from engines.paper_engine import is_paper_order_id
        if is_paper_order_id(order_id):
            return {"ok": False, "reason": "该委托号属于「模拟盘」（本地撮合），"
                                           "券商柜台不存在这笔委托，无可撤销的挂单。"}
        b = get_bridge(broker_id or None)
        return await execution.cancel_order(b, order_id)

    @mcp.tool()
    async def cancel_order_price(order_id: str, deviation: float = 0.01,
                                 broker_id: str = "") -> dict:
        """超价撤单（偏离最新价超过 deviation 撤）。"""
        from engines.paper_engine import is_paper_order_id
        if is_paper_order_id(order_id):
            return {"ok": False, "reason": "该委托号属于「模拟盘」（本地撮合），"
                                           "券商柜台不存在这笔委托，无可撤销的挂单。"}
        b = get_bridge(broker_id or None)
        return await execution.cancel_order_price(b, order_id, deviation)

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
