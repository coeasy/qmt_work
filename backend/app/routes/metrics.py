from app.routes._common import ok, err, state

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus 格式指标端点（非 loopback 需有效 API Key）。"""
    if state.metrics is None:
        return err(503, "指标未初始化")
    snap = {
        "ws_clients": state.ws_manager.client_count() if state.ws_manager else 0,
        "brokers": [(c.cfg.conn_id, c.connected) for c in state.broker_manager.all_connections()],
    }
    from fastapi.responses import Response
    return Response(content=state.metrics.render(snap), media_type="text/plain; version=0.0.4")

@router.get("/quote-bus/stats")
async def quote_bus_stats():
    out = {"bus": state.quote_bus.stats() if state.quote_bus else {"mode": "none"}}
    if state.sync_engine:
        out["latency"] = state.sync_engine.latency_stats()
        out["subscribed_codes"] = sorted(state.sync_engine._subscribed_codes)
    return ok(out)


# ---------------- 统一信号入口 ----------------

