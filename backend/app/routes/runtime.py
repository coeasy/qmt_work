"""G6 任务运行时 REST 端点。

- ``POST /runtime/jobs``：提交任务 {kind, name, params, priority}（写操作域）。
- ``GET /runtime/jobs``：任务列表（auto-safe → 自动暴露 MCP）。
- ``GET /runtime/jobs/{job_id}``：状态 + 进度（自动暴露）。
- ``POST /runtime/jobs/{job_id}/cancel``：取消。

kind 接入：sync（全市场同步）/ screen（条件选股）/ backtest（回测）。
"""
from typing import Any, Dict

from fastapi import APIRouter

from app.routes._common import audit_log, err, ok
from app.runtime.jobs import (
    JobSpec,
    backtest_runner,
    get_runtime,
    screen_runner,
    sync_runner,
)

router = APIRouter()

_KINDS = {"sync": sync_runner, "screen": screen_runner, "backtest": backtest_runner}


@router.post("/runtime/jobs")
async def runtime_jobs_submit(body: Dict[str, Any]):
    """提交任务：{kind: sync|screen|backtest, name, params, priority}。"""
    kind = str((body or {}).get("kind") or "").lower()
    if kind not in _KINDS:
        return err(400, f"kind 非法：{kind}（可选 {sorted(_KINDS)}）")
    params = body.get("params") or {}
    if kind == "screen" and not isinstance(params.get("conditions"), dict):
        return err(400, "screen 任务 params 须含 conditions 条件树对象")
    rid = get_runtime().submit(JobSpec(
        kind=kind,
        name=str(body.get("name") or kind),
        runner=_KINDS[kind](params),
        priority=int(body.get("priority") or 5),
        params=params,
    ))
    audit_log("api", "runtime_jobs_submit", f"job {rid}", body)

    return ok({"id": rid, "status": "queued"})


@router.get("/runtime/jobs")
async def runtime_jobs_list():
    """任务列表（最近优先）。"""
    return ok({"items": get_runtime().list(), "count": len(get_runtime().list())})


@router.get("/runtime/jobs/{job_id}")
async def runtime_jobs_get(job_id: str):
    """任务状态 + 进度：{status, progress, message, result/error, ...}。"""
    job = get_runtime().get(job_id)
    if job is None:
        return err(404, f"任务不存在：{job_id}")
    return ok(job)


@router.post("/runtime/jobs/{job_id}/cancel")
async def runtime_jobs_cancel(job_id: str):
    """取消任务（仅 queued/running 可取消）。"""
    cancelled = await get_runtime().cancel(job_id)
    if not cancelled:
        return err(400, f"任务不可取消（不存在或已终态）：{job_id}")
    audit_log("api", "runtime_jobs_cancel", job_id, None)

    return ok({"id": job_id, "status": "canceled"})
