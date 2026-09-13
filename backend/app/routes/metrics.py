from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/metrics")
async def prometheus_metrics(ctx: AppContext = Depends(get_ctx)):
    """Prometheus 格式指标端点（非 loopback 需有效 API Key）。"""
    if ctx.metrics is None:
        return err(503, "指标未初始化")
    snap = {
        "ws_clients": ctx.ws_manager.client_count() if ctx.ws_manager else 0,
        "brokers": [(c.cfg.conn_id, c.connected) for c in ctx.broker_manager.all_connections()],
    }
    from fastapi.responses import Response
    return Response(content=ctx.metrics.render(snap), media_type="text/plain; version=0.0.4")

@router.get("/quote-bus/stats")
async def quote_bus_stats(ctx: AppContext = Depends(get_ctx)):
    """获取quote-bus / stats（GET /quote-bus/stats）。"""
    out = {"bus": ctx.quote_bus.stats() if ctx.quote_bus else {"mode": "none"}}
    if ctx.sync_engine:
        out["latency"] = ctx.sync_engine.latency_stats()
        out["subscribed_codes"] = sorted(ctx.sync_engine._subscribed_codes)
    return ok(out)


# ---------------- 统一信号入口 ----------------

