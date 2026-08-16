"""FastMCP 服务：构建 MCP 实例并注册全部工具（覆盖 EzQmt + QMT-MCP 全部逻辑）。

返回的 `app` 为 Starlette ASGI 应用，挂载进 FastAPI（端点 /mcp）。
工具统一走真实券商 SDK（经 BrokerManager），无 mock。
"""
from fastmcp import FastMCP

from app.state import state
from tools.account import register_account_tools
from tools.algo import register_algo_tools
from tools.analysis import register_analysis_tools
from tools.backtest import register_backtest_tools
from tools.condition_order import register_condition_tools
from tools.factor_research import register_research_tools
from tools.limitup import register_limitup_tools
from tools.market import register_market_tools
from tools.position import register_position_tools
from tools.rebalance import register_rebalance_tools
from tools.reference import register_reference_tools
from tools.strategy_gen import register_strategy_tools
from tools.target_portfolio import register_target_portfolio_tools
from tools.trading import register_trading_tools

_INSTRUCTIONS = (
    "qmt_work 量化平台工具集（真实券商接入，无 mock）。可用于：\n"
    "行情：get_quote/get_full_tick/get_kline/get_tick/get_stock_list/search_stocks/l2_transactions\n"
    "交易：place_order/cancel_order/cancel_order_price/query_position/query_cash/query_orders/query_deals"
    "（下单前过统一风控，支持幂等键防重）\n"
    "账户：monitor_account/account_status\n"
    "再平衡：generate_rebalance（等权篮子+阈值+拆单+涨跌停处理）\n"
    "目标仓位：order_target_position（按目标市值占比调仓）\n"
    "条件单：condition_submit/condition_cancel/condition_list（价格触发，止损/突破）\n"
    "算法单：algo_submit/algo_pause/algo_resume/algo_cancel/algo_list（TWAP/VWAP 时间拆单）\n"
    "涨停监控：limitup_pool_add/limitup_pool_remove/limitup_start/limitup_stop/limitup_status（打板助手）\n"
    "回测与对比：run_backtest/compare_backtests/sensitivity_analysis（ma_cross/macd/rsi，真实 K 线+成本模型）\n"
    "研究深度：factor_ic_analysis/factor_quantile_analysis/factor_correlation_matrix/portfolio_backtest/walk_forward_analysis/attribute_performance（阶段3：因子IC/ICIR/分位/组合回测/walk-forward/归因）\n"
    "绩效分析：analyze_slippage/analyze_contribution/monthly_pnl/net_value_series\n"
    "参考数据：trading_calendar/sector_list/sector_stocks/financial_summary\n"
    "策略库：generate_strategy/save_qmt_strategy（ma_cross/macd/rsi/limitup 模板，写入 QMT 客户端）\n"
    "目标持仓：target_portfolio_sync（差量同步，支持 weight/volume 模式 + dry_run 预演）\n"
    "券商：list_brokers/list_broker_profiles/broker_status\n"
    "交易类操作（place_order/cancel_order/generate_rebalance/algo_submit/order_target_position/"
    "condition_submit/limitup_start 自动买入）必须先与用户确认方向、数量、价格后再执行。"
)


def register_broker_tools(mcp):
    @mcp.tool()
    async def list_brokers() -> list[dict]:
        """列出已配置的券商连接及其连接状态。"""
        return state.broker_manager.status_list()

    @mcp.tool()
    async def list_broker_profiles() -> list[dict]:
        """列出平台支持的券商档案（国金/华鑫/银河/同花顺/恒生PTrade/掘金…）。"""
        from xtquant_client.registry import list_profiles
        return [{"id": p.id, "name": p.name, "adapter": p.adapter,
                 "supported_account_types": p.supported_account_types,
                 "supported_periods": p.supported_periods, "sdk_required": p.sdk_required,
                 "note": p.note} for p in list_profiles()]

    @mcp.tool()
    async def broker_status() -> dict:
        """当前活跃券商连接状态。"""
        b = state.broker_manager.active_bridge()
        if not b:
            return {"connected": False, "detail": "未连接任何券商客户端"}
        return b.gateway.test_connection()


def build_mcp(risk) -> FastMCP:
    mcp = FastMCP("qmt_work", instructions=_INSTRUCTIONS)
    register_market_tools(mcp)
    register_trading_tools(mcp, risk)
    register_account_tools(mcp)
    register_backtest_tools(mcp)
    register_rebalance_tools(mcp)
    register_analysis_tools(mcp)
    register_research_tools(mcp)
    register_broker_tools(mcp)
    register_reference_tools(mcp)
    register_limitup_tools(mcp)
    register_algo_tools(mcp)
    register_strategy_tools(mcp)
    register_condition_tools(mcp)
    register_position_tools(mcp, risk)
    register_target_portfolio_tools(mcp)
    register_agent_tools(mcp)
    return mcp


def register_agent_tools(mcp):
    """阶段 5 Agent 工具：对话 + 会话列表（缺 LLM 配置返回明确文本错误，绝不造假）。"""
    from app.config import settings
    from app.state import state
    from agent.core import AgentCore
    from agent.default_tools import build_default_registry
    from agent.errors import AgentNotConfigured
    from agent.providers import build_provider

    def _core_or_err():
        if not settings.agent_enabled or not settings.agent_api_key:
            return None, "Agent 未配置：请在「设置」开启 agent_enabled 并配置 LLM API Key。"
        try:
            provider = build_provider(settings.agent_provider, settings.agent_api_key,
                                      settings.agent_model, settings.agent_base_url)
            return AgentCore(state.db, provider, build_default_registry()), None
        except AgentNotConfigured as exc:
            return None, str(exc)

    @mcp.tool()
    async def agent_chat(message: str, session_id: int = 0, conn_id: str = "") -> dict:
        """与 qmt_work 智能助手对话（基于真实券商/运行期数据查询工具）。

        - message: 用户问题（必填）
        - session_id: 历史会话 id（可选，省略则新建）
        - conn_id: 指定券商连接（可选，省略用活跃连接）
        """
        core, err_msg = _core_or_err()
        if core is None:
            return {"error": err_msg}
        result = await core.chat(message, session_id=session_id or None,
                                 conn_id=conn_id or None)
        return result

    @mcp.tool()
    async def agent_sessions() -> dict:
        """列出 Agent 历史会话。"""
        core, err_msg = _core_or_err()
        if core is None:
            return {"error": err_msg}
        return {"sessions": core.list_sessions()}
