from app.routes._common import ok, err, state

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
from pathlib import Path



router = APIRouter()

@router.post("/reconcile", tags=["reconcile"], summary="立即执行委托对账核销")
async def reconcile_now(body: dict | None = None):
    """比对 WAL 未核销委托与券商当日委托/成交，标记最终状态并写核销记录。"""
    if state.reconciler is None:
        return err(503, "对账器未初始化")
    conn_id = (body or {}).get("conn_id") or None
    try:
        res = await state.reconciler.reconcile(conn_id)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"对账失败：{exc}")
    return ok(res)

@router.get("/reconcile/last", tags=["reconcile"], summary="最近一次对账结果")
async def reconcile_last():
    if state.reconciler is None:
        return err(503, "对账器未初始化")
    return ok(state.reconciler.last_result or {"checked": 0})

@router.get("/wal/stats", tags=["reconcile"], summary="WAL 统计与轮转状态")
async def wal_stats():
    if state.wal is None:
        return err(503, "WAL 未初始化")
    p = Path(state.wal.path)
    snap = p.with_suffix(".snapshot.jsonl")
    recs = state.wal.all_records()
    by_entity: dict[str, int] = {}
    for r in recs:
        e = r.get("entity", "unknown")
        by_entity[e] = by_entity.get(e, 0) + 1
    return ok({
        "path": str(p),
        "size": p.stat().st_size if p.exists() else 0,
        "snapshot_size": snap.stat().st_size if snap.exists() else 0,
        "checkpoint_threshold": state.wal._threshold,
        "records": len(recs),
        "by_entity": by_entity,
    })

@router.post("/wal/checkpoint", tags=["reconcile"], summary="手动触发 WAL 归档轮转")
async def wal_checkpoint():
    if state.wal is None:
        return err(503, "WAL 未初始化")
    try:
        state.wal.checkpoint()
    except Exception as exc:  # noqa: BLE001
        return err(500, f"归档失败：{exc}")
    return ok({"checkpointed": True})


# ---------------- 行情共享总线状态 ----------------

