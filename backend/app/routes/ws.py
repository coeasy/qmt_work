import uuid

from fastapi import APIRouter

from app.middleware.request_id import _request_id_ctx
from app.routes._common import WebSocket, WebSocketDisconnect, _ws_authorized, state

# --- stdlib imports injected by fix_route_imports ---
import logging

router = APIRouter()

log = logging.getLogger("qmt_work.ws")

@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # T11：WS 路径同样注入 request_id（http 中间件不覆盖 WS），
    # 该连接生命周期内的日志自动带 [<id>]，便于按连接聚合排障。
    rid = ws.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = _request_id_ctx.set(rid)
    try:
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
            # 裸吞会让客户端异常（消息解析/处理器错误）完全不可见，此处补一条警告日志。
            log.exception("ws client %s crashed", cid)
            state.ws_manager.disconnect(cid)
    finally:
        _request_id_ctx.reset(token)

