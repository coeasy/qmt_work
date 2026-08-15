"""可观测性扩展路由（P2）：指标摘要、trace 明细、运行时模式。

挂载到 /api/v1/observability（由 main.py 集成，本文件不负责 include_router）。
"""
import time

from app.routes._common import ok, err, state
from fastapi import APIRouter, Query

router = APIRouter(prefix="/observability")


@router.get("/metrics-summary")
async def metrics_summary():
    """关键计数器汇总，便于前端/看板快速读取。"""
    m = state.metrics
    if m is None:
        return err(503, "指标未初始化")
    return ok({
        "uptime": round(time.time() - m.start_ts, 1),
        "orders": sum(m._orders.values()) if hasattr(m, "_orders") else 0,
        "quotes": m._quotes if hasattr(m, "_quotes") else 0,
        "backtests": sum(m._backtests.values()) if hasattr(m, "_backtests") else 0,
        "paper_orders": sum(m._paper_orders.values()) if hasattr(m, "_paper_orders") else 0,
        "ws_clients": m._ws_clients if hasattr(m, "_ws_clients") else 0,
        "errors": sum(m._errors.values()) if hasattr(m, "_errors") else 0,
    })


@router.get("/traces")
async def traces(limit: int = Query(50, ge=1, le=1000)):
    """最近请求 trace 环形缓冲（默认最近 50 条）。"""
    m = state.metrics
    if m is None:
        return err(503, "指标未初始化")
    return ok(m.recent_traces()[-limit:])


@router.get("/runtime")
async def runtime():
    """每个券商连接的运行时模式（in_process/bridge/unknown）。"""
    bm = state.broker_manager
    if bm is None:
        return err(503, "券商管理器未初始化")
    try:
        conns = bm.all_connections()
    except Exception as exc:  # 防御：管理器异常时不抛 500
        return err(503, f"读取券商连接失败: {exc}")
    out = []
    for conn in conns:
        cfg = conn.cfg
        mode = getattr(getattr(conn, "adapter", None), "runtime_mode", None)
        if mode not in ("in_process", "bridge", "unknown"):
            mode = "unknown"
        out.append({
            "conn_id": getattr(cfg, "conn_id", "") or "",
            "name": getattr(cfg, "name", "") or "",
            "broker": getattr(cfg, "broker_id", "") or "",
            "runtime_mode": mode,
        })
    return ok(out)
