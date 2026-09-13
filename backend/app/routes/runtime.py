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
from app.runtime.jobs import JobSpec, get_runtime

router = APIRouter()


@router.post("/runtime/jobs")
async def runtime_jobs_submit(body: Dict[str, Any]):
    """提交任务：{kind: system.*|sync|screen|backtest, name, params, priority}。"""
    body = body or {}
    kind = str(body.get("kind") or "").lower()
    factory = _runner_for_kind(kind)
    if factory is None:
        return err(400, f"kind 非法或无对应 runner：{kind}")
    params = body.get("params") or {}
    if kind == "screen" and not isinstance(params.get("conditions"), dict):
        return err(400, "screen 任务 params 须含 conditions 条件树对象")
    rid = get_runtime().submit(JobSpec(
        kind=kind,
        name=str(body.get("name") or kind),
        runner=factory(params),
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


# ============================================================================
# V9 Phase 7：Schedule CRUD（Durable Scheduler）
# ============================================================================

def _store():
    from app.runtime.schedules import ScheduleStore
    from core.db import get_db
    return ScheduleStore(get_db())


def _runner_for_kind(kind: str):
    """kind -> runner 工厂（system.* 由 system_jobs 提供，内置 4 类回退）。"""
    from app.runtime.jobs import runner_factory_for
    from app.runtime.system_jobs import runner_for as system_runner_for
    return system_runner_for(kind) or runner_factory_for(kind)


@router.post("/runtime/schedules")
async def schedules_create(body: Dict[str, Any]):
    """创建调度：{kind, cron, name?, misfire_policy?, enabled?, params?}。"""
    from app.runtime.cron import CronExpr
    body = body or {}
    kind = str(body.get("kind") or "")
    cron = str(body.get("cron") or "")
    if not kind:
        return err(400, "kind 必填")
    try:
        CronExpr.parse(cron)
    except ValueError as exc:
        return err(400, f"cron 非法：{exc}")
    if _runner_for_kind(kind) is None:
        return err(400, f"kind 无对应 runner：{kind}")
    try:
        row = _store().create(
            kind, cron, name=str(body.get("name") or ""),
            misfire_policy=str(body.get("misfire_policy") or "coalesce"),
            enabled=bool(body.get("enabled", True)),
            params=body.get("params") or {})
    except ValueError as exc:
        return err(400, str(exc))
    audit_log("api", "schedules_create", row["id"], body)

    return ok(row)


@router.get("/runtime/schedules")
async def schedules_list(enabled_only: bool = False):
    """调度列表（enabled_only=true 仅返回启用项）。"""
    items = _store().list(enabled_only=enabled_only)

    return ok({"items": items, "count": len(items)})


@router.get("/runtime/schedules/{schedule_id}")
async def schedules_get(schedule_id: str):
    row = _store().get(schedule_id)
    if row is None:
        return err(404, f"调度不存在：{schedule_id}")
    return ok(row)


@router.put("/runtime/schedules/{schedule_id}")
async def schedules_update(schedule_id: str, body: Dict[str, Any]):
    """更新调度（cron/name/enabled/misfire_policy/params）。"""
    row = _store().update(schedule_id, **(body or {}))
    if row is None:
        return err(404, f"调度不存在：{schedule_id}")
    audit_log("api", "schedules_update", schedule_id, body)

    return ok(row)


@router.delete("/runtime/schedules/{schedule_id}")
async def schedules_delete(schedule_id: str):
    deleted = _store().delete(schedule_id)
    if not deleted:
        return err(404, f"调度不存在：{schedule_id}")
    audit_log("api", "schedules_delete", schedule_id, None)

    return ok({"id": schedule_id, "deleted": True})


@router.post("/runtime/schedules/{schedule_id}/trigger")
async def schedules_trigger(schedule_id: str):
    """手动立即触发一次（不计入 misfire 相位推进）。"""
    row = _store().get(schedule_id)
    if row is None:
        return err(404, f"调度不存在：{schedule_id}")
    factory = _runner_for_kind(row["kind"])
    if factory is None:
        return err(400, f"kind 无对应 runner：{row['kind']}")
    params = dict(row.get("params") or {})
    params.setdefault("_schedule_id", row["id"])
    rid = get_runtime().submit(JobSpec(
        kind=row["kind"], name=f"manual:{row.get('name') or row['kind']}",
        runner=factory(params), priority=2, params=params))
    audit_log("api", "schedules_trigger", schedule_id, {"job": rid})

    return ok({"schedule_id": schedule_id, "job_id": rid})
