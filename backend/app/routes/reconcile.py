from core.context import AppContext, get_ctx
# --- stdlib imports injected by fix_route_imports ---
from pathlib import Path

from fastapi import APIRouter, Depends

from app.routes._common import err, ok

router = APIRouter()

@router.post("/reconcile", tags=["reconcile"], summary="立即执行委托对账核销")
async def reconcile_now(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """比对 WAL 未核销委托与券商当日委托/成交，标记最终状态并写核销记录。"""
    if ctx.reconciler is None:
        return err(503, "对账器未初始化")
    conn_id = (body or {}).get("conn_id") or None
    try:
        res = await ctx.reconciler.reconcile(conn_id)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"对账失败：{exc}")
    return ok(res)

@router.get("/reconcile/last", tags=["reconcile"], summary="最近一次对账结果")
async def reconcile_last(ctx: AppContext = Depends(get_ctx)):
    """获取reconcile / last（GET /reconcile/last）。"""
    if ctx.reconciler is None:
        return err(503, "对账器未初始化")
    return ok(ctx.reconciler.last_result or {"checked": 0})

# 前端 api.js 走 /reconcile/wal/*（与本模块 /reconcile、/reconcile/last 同前缀）；
# 多语言接入指南与旧调用方走 /wal/*。两个路径都注册，避免契约漂移导致
# 审计对账页 WAL 面板 404 后前端静默吞错显示空白（2026-09-12 实测缺陷）。
@router.get("/reconcile/wal/stats", tags=["reconcile"], summary="WAL 统计与轮转状态",
            operation_id="reconcile_wal_stats")
@router.get("/wal/stats", tags=["reconcile"], summary="WAL 统计与轮转状态（兼容旧路径）",
            operation_id="wal_stats_legacy")
async def wal_stats(ctx: AppContext = Depends(get_ctx)):
    """获取reconcile / wal / stats（GET /reconcile/wal/stats 与 /wal/stats）。"""
    if ctx.wal is None:
        return err(503, "WAL 未初始化")
    p = Path(ctx.wal.path)
    snap = p.with_suffix(".snapshot.jsonl")
    recs = ctx.wal.all_records()
    by_entity: dict[str, int] = {}
    for r in recs:
        e = r.get("entity", "unknown")
        by_entity[e] = by_entity.get(e, 0) + 1
    return ok({
        "path": str(p),
        "size": p.stat().st_size if p.exists() else 0,
        "snapshot_size": snap.stat().st_size if snap.exists() else 0,
        "checkpoint_threshold": ctx.wal._threshold,
        "records": len(recs),
        "by_entity": by_entity,
    })

@router.post("/reconcile/wal/checkpoint", tags=["reconcile"], summary="手动触发 WAL 归档轮转",
             operation_id="reconcile_wal_checkpoint")
@router.post("/wal/checkpoint", tags=["reconcile"], summary="手动触发 WAL 归档轮转（兼容旧路径）",
             operation_id="wal_checkpoint_legacy")
async def wal_checkpoint(ctx: AppContext = Depends(get_ctx)):
    """创建/提交reconcile / wal / checkpoint（POST /reconcile/wal/checkpoint 与 /wal/checkpoint）。"""
    if ctx.wal is None:
        return err(503, "WAL 未初始化")
    try:
        ctx.wal.checkpoint()
    except Exception as exc:  # noqa: BLE001
        return err(500, f"归档失败：{exc}")
    return ok({"checkpointed": True})


# ---------------- 行情共享总线状态 ----------------

