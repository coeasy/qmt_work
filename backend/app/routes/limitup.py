from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import audit_log, err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/limitup/status")
async def limitup_status(ctx: AppContext = Depends(get_ctx)):
    """获取limitup / status（GET /limitup/status）。

    ★ 监控未初始化时**必须报错**，不能伪装成「未运行 + 空池」。
    曾在这里兜底返回 `{"running": False, "pool": []}`，界面于是显示「监控未启动」——
    看起来一切正常、点一下就能启动；但同模块的 pool / start / stop 全都返回 503
    「涨停监控未初始化」，口径自相矛盾，用户点了才发现根本用不了。
    「未启动」与「不可用」是两件事，混为一谈就是**假空态**。
    """
    m = ctx.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化（同模块的 pool / start / stop 均不可用）")
    return ok(m.status())

@router.post("/limitup/pool")
async def limitup_pool_add(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交limitup / pool（POST /limitup/pool）。"""
    m = ctx.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    try:
        audit_log("api", "limitup_pool_add", body.get('code',''), body)

        return ok(m.add(body.get("code", "")))
    except ValueError as exc:
        return err(400, str(exc))

@router.delete("/limitup/pool")
async def limitup_pool_remove(code: str, ctx: AppContext = Depends(get_ctx)):
    """删除limitup / pool（DELETE /limitup/pool）。"""
    m = ctx.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    m.remove(code)
    return ok({"removed": code})

@router.post("/limitup/start")
async def limitup_start(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交limitup / start（POST /limitup/start）。"""
    m = ctx.limitup_monitor
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
async def limitup_stop(ctx: AppContext = Depends(get_ctx)):
    """创建/提交limitup / stop（POST /limitup/stop）。"""
    m = ctx.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    return ok(await m.stop())

@router.post("/limitup/reset")
async def limitup_reset(ctx: AppContext = Depends(get_ctx)):
    """创建/提交limitup / reset（POST /limitup/reset）。"""
    m = ctx.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    m.reset_triggered()
    return ok({"reset": True})


# ---------------- 算法单（TWAP/VWAP） ----------------

