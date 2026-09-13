from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.post("/target-portfolio/sync")
async def target_portfolio_sync(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交target-portfolio / sync（POST /target-portfolio/sync）。"""
    from tools.target_portfolio import TargetPortfolioEngine
    engine = TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db)
    res = await engine.sync(
        body.get("targets", {}),
        float(body.get("total_capital", 0) or 0),
        body.get("mode", "volume"),
        body.get("broker_id", ""),
        bool(body.get("dry_run", False)))
    if isinstance(res, dict) and res.get("ok"):
        return ok(res)
    return err(503, res.get("reason", "同步失败") if isinstance(res, dict) else "同步失败")

@router.get("/target-portfolio/plans")
async def target_portfolio_list(ctx: AppContext = Depends(get_ctx)):
    """获取target-portfolio / plans（GET /target-portfolio/plans）。"""
    from tools.target_portfolio import TargetPortfolioEngine
    return ok(TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db).list_plans())

@router.post("/target-portfolio/plans")
async def target_portfolio_save(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交target-portfolio / plans（POST /target-portfolio/plans）。"""
    from tools.target_portfolio import TargetPortfolioEngine
    nid = TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db).save_plan(
        body.get("name", ""), body.get("weights", {}))
    return ok({"id": nid})

@router.delete("/target-portfolio/plans/{pid}")
async def target_portfolio_delete(pid: int, ctx: AppContext = Depends(get_ctx)):
    """删除target-portfolio / plans（DELETE /target-portfolio/plans/{pid}）。"""
    from tools.target_portfolio import TargetPortfolioEngine
    TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db).delete_plan(pid)
    return ok({"deleted": True})

@router.post("/target-portfolio/plans/batch-delete")
async def target_portfolio_batch_delete(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交target-portfolio / plans / batch-delete（POST /target-portfolio/plans/batch-delete）。"""
    from tools.target_portfolio import TargetPortfolioEngine
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    eng = TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db)
    for pid in ids:
        eng.delete_plan(pid)
    return ok({"deleted": len(ids)})