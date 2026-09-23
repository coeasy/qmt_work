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
    # ★★ 失败原因必须按 `broker_unavailable` 分流，**不能一律 503**（2026-09-23 R25）。
    #
    # 旧实现对**所有**失败返 503「服务不可用」。而 `engine.sync()` 的失败原因绝大多数是
    # **业务性**的：`未知 mode：'ratio'`（参数写错）、targets 为空、总资金为 0……
    # 全被报成「服务不可用」⇒ 用户去查后端，而真正原因（参数错）永不出现。
    # 尤其致命的是：R25 刚把「未知 mode」从「静默按股数处理（⇒ 全仓清仓）」改成
    # **显式报错**，如果出口仍是 503，这条修复的提示就会被归因错埋掉。
    #
    # 语义边界（与 `signal.py` / `signal_router.py` 完全一致，唯一真源）：
    #   - `broker_unavailable=True` ⇒ **503**：券商客户端没连上，给「去连接」引导；
    #   - 其余（未知 mode / 参数错 / 计划不存在）⇒ **400**：请求被业务规则拒绝。
    if isinstance(res, dict) and res.get("broker_unavailable"):
        return err(503, res.get("reason", "未连接券商客户端"), res)
    return err(400, res.get("reason", "同步失败") if isinstance(res, dict) else "同步失败")

@router.get("/target-portfolio/plans")
async def target_portfolio_list(ctx: AppContext = Depends(get_ctx)):
    """获取target-portfolio / plans（GET /target-portfolio/plans）。"""
    from tools.target_portfolio import TargetPortfolioEngine
    return ok(TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db).list_plans())

@router.post("/target-portfolio/plans")
async def target_portfolio_save(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交target-portfolio / plans（POST /target-portfolio/plans）。

    ★ **带 id 即更新**（与 notifications / alerts / webhooks 同一套约定）：
    不新增 PUT 路由就不会动到契约基线与 MCP 能力清单。
    ⚠️ 更新不存在 id 时返回 ``id=0``（不是新建）—— 界面须据此提示「计划已不存在」。
    """
    from tools.target_portfolio import TargetPortfolioEngine
    nid = TargetPortfolioEngine(ctx.broker_manager, ctx.signal_router, ctx.db).save_plan(
        body.get("name", ""), body.get("weights", {}), int(body.get("id") or 0))
    if int(body.get("id") or 0) and not nid:
        return err(404, f"计划 #{body.get('id')} 不存在（可能已被删除）")
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