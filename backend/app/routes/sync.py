from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.post("/sync/subscribe")
async def sync_subscribe(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交sync / subscribe（POST /sync/subscribe）。"""
    codes = body.get("codes", [])
    if codes:
        ctx.sync_engine.client_subscribe("api", codes)
    return ok({"subscribed": sorted(ctx.sync_engine._subscribed_codes)})


# ---------------- WebSocket 统一通道 ----------------
# 鉴权：loopback（本机）免 Key；远程连接必须带 token（query 参数或 Sec-WebSocket-Protocol 头）