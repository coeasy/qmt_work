"""调度器 REST 路由（P2）：定时任务管理 + 手动触发 + 优雅停机/重启请求。

仅供 integrator 接线使用：本文件不修改 main.py / __init__.py / state.py，
由 integrator 将 `state.scheduler` 设为已 init 的 TaskScheduler 实例，并挂载本 router。
"""
import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from app.routes._common import ok, err, state

router = APIRouter()


def _scheduler():
    """取调度器；未初始化返回 None（路由据此返回 503）。"""
    return getattr(state, "scheduler", None)


class AddTaskBody(BaseModel):
    name: str
    cron: str | dict
    action: str
    payload: dict | None = None
    enabled: bool = True


class EnableBody(BaseModel):
    enabled: bool = True


@router.get("/scheduler/tasks")
async def list_tasks():
    """列出全部定时任务。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    return ok(s.list_tasks())


@router.post("/scheduler/tasks")
async def add_task(body: AddTaskBody):
    """新增定时任务（持久化并计算 next_run）。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    rec = s.add_task(body.name, body.cron, body.action,
                     body.payload, enabled=body.enabled)
    return ok(rec)


@router.delete("/scheduler/tasks/{task_id}")
async def remove_task(task_id: str):
    """删除指定任务。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    return ok({"deleted": s.remove_task(task_id)})


@router.post("/scheduler/tasks/{task_id}/enable")
async def enable_task(task_id: str, body: EnableBody):
    """启用/停用指定任务。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    rec = s.enable_task(task_id, body.enabled)
    if rec is None:
        return err(404, "任务不存在")
    return ok(rec)


@router.post("/scheduler/run-due")
async def run_due():
    """手动触发：执行所有到点的任务。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    return ok(s.run_due())


@router.post("/scheduler/shutdown")
async def shutdown():
    """请求优雅停机：置位标志位 + 广播 WS 系统停机事件（不在此终止进程）。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    s.request_shutdown()
    ws = getattr(state, "ws_manager", None)
    if ws is not None:
        try:
            await ws.broadcast("system", {
                "event": "shutdown",
                "message": "收到停机请求，正在准备优雅关闭",
            })
        except Exception:  # noqa: BLE001
            pass
    return ok({"scheduled": True})


@router.post("/scheduler/restart")
async def restart():
    """请求重启：置位标志位（由 integrator 真正执行重启）。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    s.request_restart()
    return ok({"scheduled": True})


@router.get("/scheduler/status")
async def status():
    """调度器状态：停机/重启请求标志 + 任务总数。"""
    s = _scheduler()
    if s is None:
        return err(503, "调度器未初始化")
    return ok({
        "shutdown_requested": s.shutdown_requested,
        "restart_requested": s.restart_requested,
        "task_count": len(s.list_tasks()),
    })
