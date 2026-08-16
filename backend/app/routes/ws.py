from app.routes._common import state, WebSocket, WebSocketDisconnect, _ws_authorized

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _ws_authorized(ws):
        await ws.close(code=4401, reason="unauthorized: missing/invalid token")
        return
    cid = await state.ws_manager.connect(ws)   # connect 内已发送首帧全量快照
    try:
        while True:
            raw = await ws.receive_text()
            await state.ws_manager.handle_client_message(cid, raw)
    except WebSocketDisconnect:
        state.ws_manager.disconnect(cid)
    except Exception:
        state.ws_manager.disconnect(cid)

