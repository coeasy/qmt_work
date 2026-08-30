"""G3-4 · 能力自描述端点。

暴露统一能力注册表，供前端命令面板 / Agent / 运维审计消费：
- GET /api/v1/capabilities           全部能力（可按 category / method 过滤）
- GET /api/v1/capabilities/summary   概览统计（各域端点数、agent_visible 数）

该端点本身列入自动暴露排除名单（不生成 tool），但出现在注册表中供审计。
"""
from fastapi import APIRouter

from app.routes._common import ok
from app.capabilities import build_capabilities

router = APIRouter()


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
