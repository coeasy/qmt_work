from core.context import AppContext, get_ctx
# --- stdlib imports injected by fix_route_imports ---
import logging
import uuid

from fastapi import APIRouter, Depends

from app.middleware.request_id import _request_id_ctx
from app.routes._common import WebSocket, WebSocketDisconnect, _ws_authorized

router = APIRouter()

log = logging.getLogger("qmt_work.ws")

@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, ctx: AppContext = Depends(get_ctx)):
    # T11：WS 路径同样注入 request_id（http 中间件不覆盖 WS），
    # 该连接生命周期内的日志自动带 [<id>]，便于按连接聚合排障。
    rid = ws.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = _request_id_ctx.set(rid)
    try:
        if not _ws_authorized(ws):
            await ws.close(code=4401, reason="unauthorized: missing/invalid token")
            return
        cid = await ctx.ws_manager.connect(ws)   # connect 内已发送首帧全量快照
        # 指标对象在循环外解析一次：``qmt_ws_messages_total`` 的生产者在此。
        # ★ 它此前**零写入** —— render 里那一行永远输出 0，运维看到的是
        #   「没有客户端发消息」，而不是「这个计数器根本没人写」。
        try:
            from gateway.metrics import get_metrics
            metrics = get_metrics()
        except Exception:  # noqa: BLE001  指标不可用时连接必须照常工作
            metrics = None
        try:
            while True:
                raw = await ws.receive_text()
                if metrics is not None:
                    try:
                        metrics.record_ws_message()
                    except Exception:  # noqa: BLE001  计数失败不得丢消息
                        pass
                await ctx.ws_manager.handle_client_message(cid, raw)
        except WebSocketDisconnect:
            ctx.ws_manager.disconnect(cid)
        except Exception:
            # 裸吞会让客户端异常（消息解析/处理器错误）完全不可见，此处补一条警告日志。
            log.exception("ws client %s crashed", cid)
            ctx.ws_manager.disconnect(cid)
    finally:
        _request_id_ctx.reset(token)

