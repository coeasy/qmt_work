"""G3 · 能力自描述与自动暴露的回归测试。

锁定三条核心不变量：
1. 能力注册表覆盖全部 REST 端点；行情缺口端点（boards/etfs/rotation/overview/indices）
   被标记为 agent_visible（即会被自动暴露为 MCP tool）。
2. 写入型 / 执行域端点绝不自动暴露（保持人工确认护栏，不放大交易风险面）。
3. 自动暴露生成器确实补齐缺口，且无重复工具名，且不覆盖已有手工 tool。
4. 漂移门禁：每个 agent_visible 的 GET 端点都有对应 MCP tool（新增路由即自动可用）。
"""

import inspect

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


def test_read_only_exec_domain_exemption_is_bounded():
    """「执行域只读豁免」必须**有边界**，不能越放越宽。

    2026-09-18 经确认放开 alerts/notifications 的 **GET** 只读端点
    （查告警规则 / 查通知既不产生委托也不动资金），但必须同时锁死：

    1. 这两个域的**写入型**仍然不暴露 —— 否则就成了安全旁路；
    2. 豁免**仅限** ``_READ_ALLOWED_EXEC_DENY_DOMAINS`` 登记的域 —— 谁再把
       trade/algo/signal 加进来，这条会立刻红（它们的 GET 可能是「查询即触发」语义）。
    """
    from app.capabilities import (_EXEC_DENY_DOMAINS,
                                  _READ_ALLOWED_EXEC_DENY_DOMAINS)

    # ① 豁免域必须是执行域的子集（不能凭空放行一个不在拒绝表里的域）
    assert _READ_ALLOWED_EXEC_DENY_DOMAINS <= _EXEC_DENY_DOMAINS
    # ② 绝不能把会产生委托的域放进豁免表
    assert not (_READ_ALLOWED_EXEC_DENY_DOMAINS & {
        "trade", "target-portfolio", "rebalance", "algo",
        "condition", "limitup", "signal",
    }), f"豁免表混入了交易执行域：{_READ_ALLOWED_EXEC_DENY_DOMAINS}"
    # ③ 这两个域必须真的存在于执行域表里（防止写错域名导致豁免静默失效）
    assert {"alerts", "notifications"} <= _READ_ALLOWED_EXEC_DENY_DOMAINS

    caps = build_capabilities()
    for dom in sorted(_READ_ALLOWED_EXEC_DENY_DOMAINS):
        gets = [c for c in caps if c.category == dom and c.method == "GET"]
        writes = [c for c in caps if c.category == dom and c.method != "GET"]
        assert gets, f"{dom} 域应有 GET 端点（豁免是否静默失效了？）"
        assert all(c.agent_visible for c in gets), \
            f"{dom} 域 GET 应已放开：" + str([c.path for c in gets
                                              if not c.agent_visible])
        assert all(not c.agent_visible for c in writes), \
            f"{dom} 域写入型绝不能暴露：" + str([c.path for c in writes
                                                 if c.agent_visible])


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


def test_capabilities_mcp_lists_tools():
    """`GET /capabilities/mcp` 必须真实自省出工具清单（可发现性）。

    ★ 为什么要锁：MCP 工具此前只能靠 `tools/list` 协议调用才可见，界面零感知。
    本端点把清单变成可浏览数据；若它静默返回空表，等于把 116 个工具重新藏起来。
    """
    from app.routes.capabilities import capabilities_mcp
    import asyncio

    out = asyncio.run(capabilities_mcp())
    assert isinstance(out, dict) and out.get("code") == 0, out
    d = out["data"]
    tools = d.get("tools") or []
    assert len(tools) >= 100, f"MCP 工具数异常：{len(tools)}"
    assert d["count"] == len(tools), "count 必须与 tools 长度一致"
    # 手工 tool 与自动暴露 tool 都应出现在清单里
    assert "get_quote_snapshot" in tools or any(t.startswith("get_") for t in tools)
    # by_prefix 是前缀计数，总和应等于总数
    assert sum(d["by_prefix"].values()) == len(tools), "by_prefix 计数与总数不符"
    # 接入信息必须齐全，否则界面无法给出可用的接入说明
    for k in ("endpoint", "transport", "auth", "bind"):
        assert d.get(k), f"接入信息缺失：{k}"


def test_capabilities_mcp_matches_baseline():
    """端点自省结果应与 MCP 协议层 `tools/list` 一致（同一真源）。

    防止出现「界面显示 116 个、Agent 实际只能调 80 个」这类双真源漂移。
    """
    import asyncio
    from app.routes.capabilities import capabilities_mcp
    from contracts.introspect import mcp_tools

    out = asyncio.run(capabilities_mcp())
    via_api = set(out["data"]["tools"])
    via_proto = set(mcp_tools())
    assert via_api == via_proto, (
        f"双真源漂移：仅端点有={sorted(via_api - via_proto)[:5]}；仅协议有={sorted(via_proto - via_api)[:5]}"
    )


# ──────────── 自动暴露工具：依赖注入参数不得变成入参 ────────────

def _tool_fn(mcp, name):
    """取到自动工具的可调用对象（FastMCP 版本兼容）。"""
    t = (getattr(mcp, "_tool_manager", None) and
         getattr(mcp._tool_manager, "_tools", {}).get(name))
    if t is None:
        return None
    return getattr(t, "fn", None) or getattr(t, "function", None)


def test_auto_tool_signature_excludes_di_params():
    """自动生成的 MCP tool **不得**把 ``ctx: AppContext = Depends(get_ctx)`` 编进签名。

    踩过的坑（2026-09-19）：能力自省把它当成普通 string 参数收进 ``param_schema``，
    缺省值被压成空串 ``""``，调用时把 ``ctx=""`` 传给 handler ⇒
    ``'str' object has no attribute 'db'``。凡是依赖 ctx 的自动工具**全部调不通**，
    而握手与 ``tools/list`` 全绿，只看握手永远发现不了。
    """
    mcp = _build_mcp()
    checked = 0
    for name in ("get_alerts_rules", "get_alerts_history",
                 "get_notifications", "get_notifications_logs"):
        fn = _tool_fn(mcp, name)
        assert fn is not None, f"自动工具缺失：{name}"
        params = inspect.signature(fn).parameters
        assert "ctx" not in params, f"{name} 把 ctx 编进签名：{list(params)}"
        checked += 1
    assert checked == 4


def test_auto_tool_call_resolves_ctx(app_client):  # noqa: ARG001
    """调用依赖 ctx 的自动工具必须真的拿到 AppContext（不得传空串进去）。

    ``app_client`` 提供完整 lifespan ⇒ 进程内已有 active context，
    ``Depends(get_ctx)`` 应当被解析成真正的 AppContext。
    """
    import asyncio
    import json as _json

    mcp = _build_mcp()
    fn = _tool_fn(mcp, "get_alerts_rules")
    assert fn is not None, "get_alerts_rules 未注册"

    res = asyncio.run(fn())
    text = _json.dumps(res, ensure_ascii=False)
    # 结构性失败（ctx 被当成空串）必须被抓到，业务性降级（503 等）不算缺陷
    assert "has no attribute" not in text, f"ctx 未正确注入：{text[:200]}"
    assert "Error calling tool" not in text, f"工具调用异常：{text[:200]}"


def test_auto_tools_all_registered():
    """护栏：自动暴露的整体数量不得因单点异常而崩塌（曾 120 → 68）。"""
    mcp = _build_mcp()
    names = set(mcp._tool_manager._tools.keys())
    assert len(names) >= 115, f"MCP 工具数异常（自动暴露可能整体失败了）：{len(names)}"
    for n in ("get_alerts_rules", "get_notifications"):
        assert n in names, f"自动工具缺失：{n}"
