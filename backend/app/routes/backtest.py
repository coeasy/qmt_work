from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.post("/backtest/jobs")
async def create_backtest_job(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交backtest / jobs（POST /backtest/jobs）。"""
    kind = body.get("kind", "backtest")
    if kind not in ("backtest", "compare", "sensitivity", "sweep"):
        return err(400, f"unknown kind: {kind}")
    job = await ctx.backtest_queue.submit(kind, body.get("params", {}))
    return ok(job)

@router.get("/backtest/jobs")
async def list_backtest_jobs(ctx: AppContext = Depends(get_ctx)):
    """获取backtest / jobs（GET /backtest/jobs）。"""
    jobs = ctx.db.query("SELECT * FROM backtest_jobs ORDER BY created_at DESC LIMIT 50")
    return ok(jobs)

@router.get("/backtest/jobs/{job_id}")
async def get_backtest_job(job_id: str, ctx: AppContext = Depends(get_ctx)):
    """获取backtest / jobs（GET /backtest/jobs/{job_id}）。"""
    job = ctx.backtest_queue.get(job_id)
    if not job:
        row = ctx.db.query_one("SELECT * FROM backtest_jobs WHERE id=?", (job_id,))
        return ok(row) if row else err(404, "job not found")
    return ok({k: v for k, v in job.items()})

@router.delete("/backtest/jobs/{job_id}")
async def cancel_backtest_job(job_id: str, ctx: AppContext = Depends(get_ctx)):
    """删除backtest / jobs（DELETE /backtest/jobs/{job_id}）。"""
    ok_flag = ctx.backtest_queue.cancel(job_id)
    return ok({"cancelled": ok_flag})

@router.post("/backtest/jobs/batch-delete")
async def batch_delete_backtest_jobs(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交backtest / jobs / batch-delete（POST /backtest/jobs/batch-delete）。"""
    ids = [str(x) for x in (body.get("ids") or []) if x not in (None, "")]
    if not ids:
        return err(400, "ids 不能为空")
    for jid in ids:
        ctx.backtest_queue.cancel(jid)
    return ok({"deleted": len(ids)})


@router.post("/backtest/sweep")
async def create_sweep_job(body: dict, ctx: AppContext = Depends(get_ctx)):
    """参数网格扫描（P1 向量化）：穷举 param_grid 组合，按夏普排序选优。

    body: {symbol, strategy, param_grid:{fast:[...],slow:[...]}, initial_capital?, count?, broker_id?}
    """
    if not body.get("param_grid"):
        return err(400, "param_grid 不能为空")
    job = await ctx.backtest_queue.submit("sweep", body)
    return ok(job)


# ---------------- 历史 K 线（缓存优先） ----------------

