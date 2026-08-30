from fastapi import APIRouter

from app.routes._common import audit_log, err, ok, state

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/limitup/status")
async def limitup_status():
    """获取limitup / status（GET /limitup/status）。"""
    m = state.limitup_monitor
    return ok(m.status() if m else {"running": False, "pool": []})

@router.post("/limitup/pool")
async def limitup_pool_add(body: dict):
    """创建/提交limitup / pool（POST /limitup/pool）。"""
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    try:
        audit_log("api", "limitup_pool_add", body.get('code',''), body)

        return ok(m.add(body.get("code", "")))
    except ValueError as exc:
        return err(400, str(exc))

@router.delete("/limitup/pool")
async def limitup_pool_remove(code: str):
    """删除limitup / pool（DELETE /limitup/pool）。"""
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    m.remove(code)
    return ok({"removed": code})

@router.post("/limitup/start")
async def limitup_start(body: dict):
    """创建/提交limitup / start（POST /limitup/start）。"""
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    try:
        audit_log("api", "limitup_start", "start", body)

        return ok(await m.start({
            "limit_pct": float(body.get("limit_pct", 0.1)),
            "cutoff": str(body.get("cutoff", "10:00")),
            "min_rise": float(body.get("min_rise", 0.03)),
            "buy_volume": int(body.get("buy_volume", 0)),
            "do_trade": bool(body.get("do_trade", False)),
            "interval": float(body.get("interval", 2.0))}))
    except ValueError as exc:
        return err(400, str(exc))

@router.post("/limitup/stop")
async def limitup_stop():
    """创建/提交limitup / stop（POST /limitup/stop）。"""
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    return ok(await m.stop())

@router.post("/limitup/reset")
async def limitup_reset():
    """创建/提交limitup / reset（POST /limitup/reset）。"""
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    m.reset_triggered()
    return ok({"reset": True})


# ---------------- 算法单（TWAP/VWAP） ----------------

