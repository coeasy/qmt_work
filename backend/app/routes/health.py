from core.context import AppContext, get_ctx
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.routes._common import err, ok
from app.version import __version__

log = logging.getLogger("qmt_work.routes.health")

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

# 本地化（避免 route 层反向 import core.state）
REQUIRED_PHASES = ("db", "engines", "watchdogs", "replay", "misc")

@router.get("/health")
async def health_check(ctx: AppContext = Depends(get_ctx)):
    """获取health（GET /health）。"""
    import time as _t
    db_ok = ctx.db is not None
    brokers = []
    try:
        for c in ctx.broker_manager.status_list():
            brokers.append({"conn_id": c["conn_id"], "broker": c["broker_name"],
                            "connected": c["connected"], "active": c["active"]})
    except (AttributeError, RuntimeError) as exc:
        # broker_manager 未初始化（启动早期）：返回空列表而非 500
        log.debug("broker_manager.status_list 失败：%s", exc)
    engines = {
        "backtest_queue": ctx.backtest_queue is not None,
        "limitup": bool(ctx.limitup_monitor and ctx.limitup_monitor.is_running()),
        "algo": ctx.algo_engine is not None,
        "ws": ctx.ws_manager is not None,
    }
    uptime = int(_t.time() - ctx.started_at) if ctx.started_at else 0
    # 交易时段状态（盘中/休眠，用于判断引擎是否高频轮询）
    try:
        from gateway.trading_session import default_session as _ts
        trading = {"mode": _ts.stats()["mode"], "active": _ts.is_active()}
    except Exception:  # noqa: BLE001
        trading = {"mode": "unknown", "active": None}
    # 标准化的 checks 汇总（pass/warn/fail），便于外部监控按组件告警
    checks = [
        {"name": "db", "status": "pass" if db_ok else "fail"},
        {"name": "ws", "status": "pass" if engines["ws"] else "warn"},
        {"name": "backtest", "status": "pass" if engines["backtest_queue"] else "warn"},
        {"name": "brokers", "status": "pass" if any(b["connected"] for b in brokers) else "warn"},
    ]
    status = "pass" if db_ok else "fail"
    return ok({
        "status": status,
        "service": "qmt_work", "version": __version__, "uptime_seconds": uptime,
        "db": db_ok, "brokers": brokers, "engines": engines,
        "trading_session": trading, "checks": checks,
        "lifecycle": {"ready": ctx.lifecycle_ready,
                       "stopping": ctx.lifecycle_stopping,
                       "phases": dict(ctx.phase_status)},
    })


@router.get("/live")
async def live_check(ctx: AppContext = Depends(get_ctx)):
    """存活探针（外部监控/编排接入）：进程活着即返回 200，不检查依赖。"""
    import time as _t
    return ok({
        "status": "ok",
        "service": "qmt_work", "version": __version__,
        "uptime_seconds": int(_t.time() - ctx.started_at) if ctx.started_at else 0,
        "checks": [{"name": "process", "status": "pass"}],
    })


# ---------------- 就绪检查（供编排/看门狗探活；非就绪返回 HTTP 503）----------------

@router.get("/ready")
async def ready_check(ctx: AppContext = Depends(get_ctx)):
    """就绪探针：DB 可读 + Required 阶段全部 ready（P1-18 修正 started 恒真）。

    判定基于 lifecycle 分层信号（core.ctx.REQUIRED_PHASES），
    Optional 阶段（broker/QMT）失败不阻断就绪。
    """
    import time as _t

    db_ok = ctx.db is not None
    if db_ok:
        try:
            ctx.db.query("SELECT 1")
        except Exception:  # noqa: BLE001
            db_ok = False
    required_phases = {n: ctx.phase_status.get(n) for n in REQUIRED_PHASES}
    started = all(v == "ready" for v in required_phases.values())
    engines = {
        "ws": ctx.ws_manager is not None,
        "backtest": ctx.backtest_queue is not None,
        "sync": ctx.sync_engine is not None,
    }
    ready = bool(db_ok and started and all(engines.values()))
    detail = {
        "ready": ready,
        "version": __version__,
        "uptime_seconds": int(_t.time() - ctx.started_at) if ctx.started_at else 0,
        "db": db_ok, "started": started, "engines": engines,
        "required_phases": required_phases,
        "lifecycle": {"ready": ctx.lifecycle_ready,
                       "stopping": ctx.lifecycle_stopping,
                       "phases": dict(ctx.phase_status)},
    }
    if not ready or not ctx.lifecycle_ready:
        return JSONResponse(status_code=503, content=err(503, "not ready", detail))
    return ok(detail)


# ---------------- 审计日志查询 ----------------

