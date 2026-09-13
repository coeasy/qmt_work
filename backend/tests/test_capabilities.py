"""G3 · 能力自描述与自动暴露的回归测试。

锁定三条核心不变量：
1. 能力注册表覆盖全部 REST 端点；行情缺口端点（boards/etfs/rotation/overview/indices）
   被标记为 agent_visible（即会被自动暴露为 MCP tool）。
2. 写入型 / 执行域端点绝不自动暴露（保持人工确认护栏，不放大交易风险面）。
3. 自动暴露生成器确实补齐缺口，且无重复工具名，且不覆盖已有手工 tool。
4. 漂移门禁：每个 agent_visible 的 GET 端点都有对应 MCP tool（新增路由即自动可用）。
"""

from app.capabilities import (
    agent_visible_reads,
    build_capabilities,
    tool_name_for,
)
from core.config import settings
from app.routes._common import err, ok
from gateway.risk import RiskManager
from mcp_server import build_mcp


def _build_mcp():
    risk = RiskManager(
        max_amount=settings.risk_max_amount, min_qty=settings.risk_min_qty,
        max_position_ratio=settings.risk_max_position_ratio,
        max_single_position_ratio=settings.risk_max_single_position_ratio,
        max_orders_per_min=settings.risk_max_orders_per_min,
        daily_amount_limit=settings.risk_daily_amount_limit,
        daily_loss_limit=settings.risk_daily_loss_limit,
        per_code_daily_orders=settings.risk_per_code_daily_orders,
        price_deviation_pct=settings.risk_price_deviation_pct,
        symbol_allow=settings.risk_symbol_allow, symbol_deny=settings.risk_symbol_deny,
    )
    return build_mcp(risk)


def test_capabilities_coverage():
    caps = build_capabilities()
    assert len(caps) >= 150, f"端点数异常：{len(caps)}"
    # 行情缺口端点必须 agent_visible
    gap_paths = {
        "/api/v1/market/boards", "/api/v1/market/etfs",
        "/api/v1/market/rotation", "/api/v1/market/overview",
        "/api/v1/market/indices",
    }
    by_path = {c.path: c for c in caps}
    for p in gap_paths:
        assert p in by_path, f"缺失端点 {p}"
        assert by_path[p].agent_visible is True, f"{p} 应可自动暴露"
        assert by_path[p].method == "GET"


def test_no_write_auto_exposed():
    caps = build_capabilities()
    # 任何 POST/PUT/DELETE 都不应 agent_visible（保持人工护栏）
    writes = [c for c in caps if c.method != "GET"]
    assert writes, "应有写入型端点"
    assert all(not c.agent_visible for c in writes), \
        "写入型端点不应自动暴露：" + str([c.path for c in writes if c.agent_visible])
    # 交易执行域的 GET 读端点也不应自动暴露（已有手工 tool + 风险面）
    trade_gets = [c for c in caps
                  if c.category in ("trade", "target-portfolio", "algo",
                                    "condition", "rebalance", "limitup")
                  and c.method == "GET"]
    assert all(not c.agent_visible for c in trade_gets), \
        "交易域 GET 不应自动暴露：" + str([c.path for c in trade_gets if c.agent_visible])


def test_auto_expose_tools():
    mcp = _build_mcp()
    names = set(mcp._tool_manager._tools.keys())
    # 缺口端点已暴露
    for n in ("get_market_boards", "get_market_etfs", "get_market_rotation",
              "get_market_overview", "get_market_indices"):
        assert n in names, f"自动 tool 缺失：{n}"
    # 手工 market tool 仍在
    for n in ("get_quote", "get_kline", "search_stocks", "get_stock_list",
              "get_full_tick", "get_tick"):
        assert n in names, f"手工 tool 丢失：{n}"
    # 交易执行域无自动 tool
    trade_auto = [n for n in names if n.startswith(("get_trade_", "get_target_portfolio_",
                                                     "get_algo_", "get_condition_"))]
    assert not trade_auto, f"交易域不应有自动 tool：{trade_auto}"
    # 无重复
    from collections import Counter
    dups = [k for k, v in Counter(list(names)).items() if v > 1]
    assert not dups, f"重复 tool 名：{dups}"


def test_capability_drift_gate():
    """G3-5 漂移门禁：每个 agent_visible GET 端点都有对应 MCP tool。"""
    mcp = _build_mcp()
    registered = set(mcp._tool_manager._tools.keys())
    missing = []
    for cap in agent_visible_reads():
        name = tool_name_for(cap)
        if name not in registered:
            missing.append((cap.path, name))
    assert not missing, f"存在未暴露的 agent_visible 端点：{missing}"


def test_capabilities_route():
    """直接调用路由 handler，验证返回结构与过滤。"""
    import asyncio

    from app.routes.capabilities import capabilities_summary, list_capabilities

    res = asyncio.run(list_capabilities(category="market", method="GET"))
    assert res["code"] == 0
    items = res["data"]["items"]
    assert res["data"]["total"] == len(items)
    assert all(c["category"] == "market" and c["method"] == "GET" for c in items)

    summ = asyncio.run(capabilities_summary())
    assert summ["code"] == 0
    assert summ["data"]["rest_total"] >= 150
    assert summ["data"]["agent_visible_total"] > 0


def test_auto_tool_unwrap():
    """验证自动 tool 的 {code,data} 解包：成功取 data，非零业务码原样返回（零 mock）。"""
    from app.capabilities import Capability, ParamSpec
    from mcp_server.auto_expose import _make_tool_func

    async def ok_ep(code: str = "600519.SH"):
        return ok({"code": code, "last": 1800.0})

    async def err_ep(code: str = "600519.SH"):
        return err(503, "未连接券商")

    cap = Capability(
        id="GET:/x", path="/api/v1/x", method="GET", category="market",
        summary="t", risk_level="low", confirm_required=False,
        agent_visible=True, tool_name="get_x",
        param_schema=[ParamSpec(name="code", type="string", required=False, default="")],
        endpoint=ok_ep,
    )
    ok_tool = _make_tool_func(ok_ep, cap, "get_x")
    import asyncio
    out = asyncio.run(ok_tool(code="000001.SZ"))
    assert out == {"code": "000001.SZ", "last": 1800.0}, out  # 解包为 data

    cap_err = Capability(
        id="GET:/y", path="/api/v1/y", method="GET", category="market",
        summary="t", risk_level="low", confirm_required=False,
        agent_visible=True, tool_name="get_y",
        param_schema=[ParamSpec(name="code", type="string", required=False, default="")],
        endpoint=err_ep,
    )
    err_tool = _make_tool_func(err_ep, cap_err, "get_y")
    out2 = asyncio.run(err_tool())
    assert isinstance(out2, dict) and out2.get("code") == 503, out2  # 非零业务码原样返回
