from app.routes._common import ok, state

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.post("/sync/subscribe")
async def sync_subscribe(body: dict):
    codes = body.get("codes", [])
    if codes:
        state.sync_engine.client_subscribe("api", codes)
    return ok({"subscribed": sorted(state.sync_engine._subscribed_codes)})


# ---------------- WebSocket 统一通道 ----------------
# 鉴权：loopback（本机）免 Key；远程连接必须带 token（query 参数或 Sec-WebSocket-Protocol 头）