"""策略运行容器路由（P0）：在平台内把生成的策略当作实盘/模拟机器人运行。

- GET  /strategies/run                 列出全部运行实例
- POST /strategies/run                 创建实例（strategy_type/code(s)/params/mode/interval…）
- GET  /strategies/run/{id}            实例详情（含运行时状态）
- POST /strategies/run/{id}/start      启动（异步循环）
- POST /strategies/run/{id}/stop       停止
- DELETE /strategies/run/{id}           删除
- GET  /strategies/run/{id}/logs       运行日志
- POST /strategies/run/precheck        风控预检（非变更型，不计入日级计数）
"""
from app.routes._common import ok, err, state

from fastapi import APIRouter

router = APIRouter()


def _rt():
    return getattr(state, "strategy_runtime", None)


_NOT_READY = "策略运行容器未初始化"


@router.get("/strategies/run")
async def list_runs():
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    return ok(rt.list_runs())


@router.post("/strategies/run")
async def create_run(body: dict):
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    try:
        return ok(rt.create(body))
    except ValueError as exc:
        return err(400, str(exc))


@router.get("/strategies/run/{run_id}")
async def get_run(run_id: int):
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    run = rt.get_run(run_id)
    if run is None:
        return err(404, "未知运行实例")
    return ok(run)


@router.post("/strategies/run/{run_id}/start")
async def start_run(run_id: int):
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    try:
        return ok(rt.start(run_id))
    except KeyError as exc:
        return err(404, str(exc))


@router.post("/strategies/run/{run_id}/stop")
async def stop_run(run_id: int):
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    return ok(rt.stop(run_id))


@router.delete("/strategies/run/{run_id}")
async def delete_run(run_id: int):
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    rt.delete(run_id)
    return ok({"deleted": True})


@router.get("/strategies/run/{run_id}/logs")
async def run_logs(run_id: int, limit: int = 100):
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    return ok(rt.logs(run_id, limit))


@router.post("/strategies/run/precheck")
async def precheck(body: dict):
    """风控预检：判断一笔委托是否会被风控放行（不计入频率窗口与日级用量）。"""
    if state.risk is None:
        return err(503, "风控未初始化")
    code = str(body.get("code", "")).strip().upper()
    direction = (body.get("direction") or "buy").lower()
    try:
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
    except (TypeError, ValueError):
        return err(400, "volume/price 必须为数字")
    allowed, reason = state.risk.precheck_order(code, price, volume, direction)
    return ok({"allowed": allowed, "reason": reason})
