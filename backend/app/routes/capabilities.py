"""G3-4 · 能力自描述端点。

暴露统一能力注册表，供前端命令面板 / Agent / 运维审计消费：
- GET /api/v1/capabilities           全部能力（可按 category / method 过滤）
- GET /api/v1/capabilities/summary   概览统计（各域端点数、agent_visible 数）

该端点本身列入自动暴露排除名单（不生成 tool），但出现在注册表中供审计。
"""
import logging

from fastapi import APIRouter

from app.capabilities import build_capabilities
from app.routes._common import ok

log = logging.getLogger("qmt_work.routes.capabilities")
router = APIRouter()


@router.get("/platform/status")
async def platform_status():
    """平台运行时能力自描述（D-C / D12）。

    返回真实交易可用性（依据券商连接态）与选股可用性（screening_ready / screening_providers）。
    前端 PlatformContext 据此驱动 ``can("trading")`` 等能力门控；未连接券商时 ``screening_ready``
    仍应为 true（只要有 eltdx / baostock / akshare / 本地数据可用），否则即违反 D12。
    """
    from app.platform import get_platform_status
    return ok(get_platform_status())


@router.get("/capabilities")
async def list_capabilities(category: str = "", method: str = ""):
    """能力注册表：每个 REST 端点归一为 {id,path,method,category,summary,risk_level,
    confirm_required,agent_visible,tool_name,param_schema}。

    category：按域过滤（market/reference/trade/...）；method：GET/POST/... 过滤。
    """
    caps = build_capabilities()
    if category:
        caps = [c for c in caps if c.category == category]
    if method:
        caps = [c for c in caps if c.method.upper() == method.upper()]
    return ok({
        "total": len(caps),
        "agent_visible": sum(1 for c in caps if c.agent_visible),
        "items": [c.to_dict() for c in caps],
    })


@router.get("/capabilities/mcp")
async def capabilities_mcp():
    """MCP 工具清单与接入信息（供界面自省与运维核对）。

    ★ 为什么要这个端点：MCP 此前**只能通过 `tools/list` 协议调用**才能看见，
    界面 `grep mcp` 零匹配 —— 用户装完客户端根本不知道自己有 115 个可被 Agent
    调用的工具，也不知道该怎么接。能力存在但不可发现，等于不存在。

    返回：
      - ``tools``：工具名清单（与 ``tests/contracts/mcp_tools.json`` 基线一致）
      - ``endpoint`` / ``transport`` / ``auth``：接入方式
      - ``by_prefix``：按前缀粗分组，便于界面浏览
    """
    tools: list[str] = []
    try:
        from mcp_server import build_mcp
        mcp = build_mcp(None)
        # FastMCP 2.x：异步 tool_manager；不同版本属性名不同，逐级兜底
        manager = getattr(mcp, "_tool_manager", None) or getattr(mcp, "tool_manager", None)
        cache = getattr(manager, "_tools", None) if manager else None
        if isinstance(cache, dict):
            tools = sorted(cache.keys())
    except Exception as exc:  # noqa: BLE001 — 自省失败只降级，不影响其它能力
        log.warning("MCP 工具清单获取失败：%s", exc)

    by_prefix: dict[str, int] = {}
    for t in tools:
        p = t.split("_", 1)[0]
        by_prefix[p] = by_prefix.get(p, 0) + 1

    from core.config import settings
    return ok({
        "tools": tools,
        "count": len(tools),
        "by_prefix": by_prefix,
        "endpoint": "/mcp",
        "transport": "streamable-http",
        "auth": "admin scope 或主密钥（X-API-Key / Bearer）；loopback 免鉴权",
        "bind": "127.0.0.1",
        "local_only_note": (
            "默认仅本地监听；远程监听 + 默认密钥会拒绝启动（run.py 的保护）"
            if getattr(settings, "api_key", "") == "qmt-dev-key" else ""
        ),
    })


@router.get("/capabilities/summary")
async def capabilities_summary():
    """能力概览：各域端点数 / 可自动暴露数 / REST 总数。"""
    caps = build_capabilities()
    by_cat: dict[str, dict] = {}
    for c in caps:
        d = by_cat.setdefault(c.category, {"total": 0, "agent_visible": 0,
                                           "write": 0, "read": 0})
        d["total"] += 1
        d["agent_visible"] += 1 if c.agent_visible else 0
        if c.method == "GET":
            d["read"] += 1
        else:
            d["write"] += 1
    return ok({
        "rest_total": len(caps),
        "agent_visible_total": sum(1 for c in caps if c.agent_visible),
        "by_category": by_cat,
    })
