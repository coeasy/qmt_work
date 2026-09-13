from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

router = APIRouter()

# ---------------- 运行时配置中心（引擎级参数热更新，配置灵活化） ----------------

@router.get("/config/runtime")
async def get_runtime_config(ctx: AppContext = Depends(get_ctx)):
    """读取全部运行时引擎参数（含生效值/默认值/说明；修改后立即生效）。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    return ok(ctx.runtime_config.all())

@router.put("/config/runtime")
async def put_runtime_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """批量更新引擎运行参数（校验类型与下限，热更新无需重启）。"""
    rc = ctx.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    try:
        changed = rc.set_many(body)
    except ValueError as exc:
        return err(400, str(exc))
    ctx.db.audit("admin", "runtime_config.update", "global",
                   {"changed": changed}, "ok")
    return ok({"saved": True, "changed": changed, "config": rc.all()})

@router.post("/config/runtime/reset")
async def reset_runtime_config(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """恢复默认：body.key 指定单个（缺省全部重置）。"""
    rc = ctx.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    key = (body or {}).get("key", "")
    changed = rc.reset(key=key or "")
    ctx.db.audit("admin", "runtime_config.reset", key or "*",
                   {"changed": changed}, "ok")
    return ok({"reset": changed, "config": rc.all()})

@router.get("/config/runtime/history")
async def get_runtime_config_history(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """变更历史（含回滚入口），按时间倒序。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    try:
        limit = max(1, min(int(limit), 200))
    except (ValueError, TypeError):
        limit = 50
    rows = ctx.runtime_config.history(limit)
    return ok({"rows": rows})

@router.post("/config/runtime/rollback")
async def rollback_runtime_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """回滚到指定历史记录 id：将该记录的旧值重新写回。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    entry_id = int((body or {}).get("id", 0))
    if not entry_id:
        return err(400, "缺少 id")
    ok_flag = ctx.runtime_config.rollback(entry_id)
    if not ok_flag:
        return err(400, "回滚失败：记录不存在或 key 非法")
    ctx.db.audit("admin", "runtime_config.rollback", str(entry_id),
                   {"id": entry_id}, "ok")
    return ok({"rolled_back": entry_id, "config": ctx.runtime_config.all()})


# ---------------- 风控配置（运行期可调，持久化 risk_config） ----------------

@router.get("/config/risk")
async def get_risk_config(ctx: AppContext = Depends(get_ctx)):
    """读取风控参数（含日级限额与熔断实时状态）。"""
    rm = ctx.risk
    if rm is None:
        return ok({
            "max_amount": 100_000.0, "min_qty": 100, "max_position_ratio": 0.3,
            "max_single_position_ratio": 0.2, "max_orders_per_min": 30,
            "daily_amount_limit": 0.0, "daily_loss_limit": 0.0,
            "per_code_daily_orders": 0})
    data = rm.to_dict()
    data["daily"] = rm.daily_stats()
    return ok(data)

@router.put("/config/risk")
async def put_risk_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """更新风控参数（持久化到 risk_config 表）。"""
    rm = ctx.risk
    if rm is None:
        return err(503, "风控未初始化")
    try:
        changed = rm.update_from(body)
    except ValueError as exc:
        return err(400, str(exc))
    rm.save_to_db(ctx.db)
    ctx.db.audit("admin", "risk_config.update", "global",
                   {"changed": changed}, "ok")
    return ok({"saved": True, "changed": changed, "config": rm.to_dict()})

@router.get("/config/risk/daily")
async def get_risk_daily(ctx: AppContext = Depends(get_ctx)):
    """日级风控实时用量与熔断状态（B4）。"""
    if ctx.risk is None:
        return err(503, "风控未初始化")
    return ok(ctx.risk.daily_stats())

@router.post("/config/risk/circuit")
async def post_risk_circuit(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """熔断开关：action=trip 手动熔断（停止买入开仓）/ action=reset 解除熔断。"""
    rm = ctx.risk
    if rm is None:
        return err(503, "风控未初始化")
    body = body or {}
    action = str(body.get("action", "reset")).lower()
    if action == "trip":
        reason = str(body.get("reason") or "人工熔断：暂停一切买入开仓")
        rm.trip(reason)
        if ctx.notifier:
            await ctx.notifier.notify("risk.circuit", "风控熔断已开启", reason,
                                        {"reason": reason, "manual": True})
    elif action == "reset":
        rm.reset_circuit()
        if ctx.notifier:
            await ctx.notifier.notify("risk.circuit", "风控熔断已解除",
                                        "已恢复买入开仓，日初净值重新锚定", {"manual": True})
    else:
        return err(400, "action 仅支持 trip / reset")
    ctx.db.audit("admin", f"risk.circuit.{action}", "global",
                   {"body": body}, "ok")
    return ok(rm.daily_stats())


# ---------------- 健康检查 ----------------

