from app.routes._common import ok, err, state
from app.version import __version__

from fastapi import APIRouter
from fastapi.responses import JSONResponse
# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/health")
async def health_check():
    import time as _t
    db_ok = state.db is not None
    brokers = []
    try:
        for c in state.broker_manager.status_list():
            brokers.append({"conn_id": c["conn_id"], "broker": c["broker_name"],
                            "connected": c["connected"], "active": c["active"]})
    except Exception:  # noqa: BLE001
        pass
    engines = {
        "backtest_queue": state.backtest_queue is not None,
        "limitup": bool(state.limitup_monitor and state.limitup_monitor.is_running()),
        "algo": state.algo_engine is not None,
        "ws": state.ws_manager is not None,
    }
    uptime = int(_t.time() - state.started_at) if state.started_at else 0
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
    })


@router.get("/live")
async def live_check():
    """存活探针（外部监控/编排接入）：进程活着即返回 200，不检查依赖。"""
    import time as _t
    return ok({
        "status": "ok",
        "service": "qmt_work", "version": __version__,
        "uptime_seconds": int(_t.time() - state.started_at) if state.started_at else 0,
        "checks": [{"name": "process", "status": "pass"}],
    })


# ---------------- 就绪检查（供编排/看门狗探活；非就绪返回 HTTP 503）----------------

@router.get("/ready")
async def ready_check():
    """就绪探针：DB 可读 + 启动完成 + 核心引擎在跑。返回 200（code=0）或 HTTP 503。"""
    import time as _t
    db_ok = state.db is not None
    if db_ok:
        try:
            state.db.query("SELECT 1")
        except Exception:  # noqa: BLE001
            db_ok = False
    started = state.started_at is not None
    engines = {
        "ws": state.ws_manager is not None,
        "backtest": state.backtest_queue is not None,
        "sync": state.sync_engine is not None,
    }
    ready = bool(db_ok and started and all(engines.values()))
    detail = {
        "ready": ready,
        "version": __version__,
        "uptime_seconds": int(_t.time() - state.started_at) if state.started_at else 0,
        "db": db_ok, "started": started, "engines": engines,
    }
    if not ready:
        return JSONResponse(status_code=503, content=err(503, "not ready", detail))
    return ok(detail)


# ---------------- 审计日志查询 ----------------

