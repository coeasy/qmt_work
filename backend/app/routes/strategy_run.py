from core.context import AppContext, get_ctx, active_context
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
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

router = APIRouter()


def _rt():
    return getattr(active_context(), "strategy_runtime", None)


_NOT_READY = "策略运行容器未初始化"


@router.get("/strategies/run")
async def list_runs(ctx: AppContext = Depends(get_ctx)):
    """获取strategies / run（GET /strategies/run）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    return ok(rt.list_runs())


@router.post("/strategies/run")
async def create_run(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交strategies / run（POST /strategies/run）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    try:
        return ok(rt.create(body))
    except ValueError as exc:
        return err(400, str(exc))


@router.get("/strategies/run/{run_id}")
async def get_run(run_id: int, ctx: AppContext = Depends(get_ctx)):
    """获取strategies / run（GET /strategies/run/{run_id}）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    run = rt.get_run(run_id)
    if run is None:
        return err(404, "未知运行实例")
    return ok(run)


@router.post("/strategies/run/{run_id}/start")
async def start_run(run_id: int, ctx: AppContext = Depends(get_ctx)):
    """创建/提交strategies / run / start（POST /strategies/run/{run_id}/start）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    try:
        return ok(rt.start(run_id))
    except KeyError as exc:
        return err(404, str(exc))


@router.post("/strategies/run/{run_id}/stop")
async def stop_run(run_id: int, ctx: AppContext = Depends(get_ctx)):
    """创建/提交strategies / run / stop（POST /strategies/run/{run_id}/stop）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    return ok(rt.stop(run_id))


@router.delete("/strategies/run/{run_id}")
async def delete_run(run_id: int, ctx: AppContext = Depends(get_ctx)):
    """删除strategies / run（DELETE /strategies/run/{run_id}）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    rt.delete(run_id)
    return ok({"deleted": True})

@router.post("/strategies/run/batch-delete")
async def batch_delete_runs(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交strategies / run / batch-delete（POST /strategies/run/batch-delete）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    for run_id in ids:
        rt.delete(run_id)
    return ok({"deleted": len(ids)})


@router.get("/strategies/run/{run_id}/logs")
async def run_logs(run_id: int, limit: int = 100, ctx: AppContext = Depends(get_ctx)):
    """获取strategies / run / logs（GET /strategies/run/{run_id}/logs）。"""
    rt = _rt()
    if rt is None:
        return err(503, _NOT_READY)
    return ok(rt.logs(run_id, limit))


@router.post("/strategies/run/precheck")
async def precheck(body: dict, ctx: AppContext = Depends(get_ctx)):
    """风控预检：判断一笔委托是否会被风控放行（不计入频率窗口与日级用量）。"""
    if ctx.risk is None:
        return err(503, "风控未初始化")
    code = str(body.get("code", "")).strip().upper()
    direction = (body.get("direction") or "buy").lower()
    try:
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
    except (TypeError, ValueError):
        return err(400, "volume/price 必须为数字")
    allowed, reason = ctx.risk.precheck_order(code, price, volume, direction)
    return ok({"allowed": allowed, "reason": reason})
