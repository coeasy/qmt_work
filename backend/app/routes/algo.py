from app.routes._common import ok, err, state

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.post("/algo/submit")
async def algo_submit(body: dict):
    e = state.algo_engine
    if e is None:
        return err(503, "算法单引擎未初始化")
    try:
        return ok(await e.submit(
            body.get("code", ""), body.get("direction", "buy"),
            int(body.get("volume", 0)), str(body.get("algo", "twap")),
            int(body.get("duration", 300)), int(body.get("slices", 5)),
            str(body.get("price_type", "market")),
            float(body.get("limit_price", 0) or 0), str(body.get("remark", "")),
            float(body.get("visible_pct", 10.0)),
            float(body.get("participation_rate", 0.1))))
    except ValueError as exc:
        return err(400, str(exc))

@router.get("/algo")
async def algo_list():
    e = state.algo_engine
    if e is None:
        return err(503, "算法单引擎未初始化")
    return ok(e.list())

@router.post("/algo/{algo_id}/pause")
async def algo_pause(algo_id: str):
    try:
        return ok(state.algo_engine.pause(algo_id))
    except KeyError as exc:
        return err(404, str(exc))

@router.post("/algo/{algo_id}/resume")
async def algo_resume(algo_id: str):
    try:
        return ok(state.algo_engine.resume(algo_id))
    except KeyError as exc:
        return err(404, str(exc))

@router.post("/algo/{algo_id}/cancel")
async def algo_cancel(algo_id: str):
    try:
        return ok(state.algo_engine.cancel(algo_id))
    except KeyError as exc:
        return err(404, str(exc))


# ---------------- 参考数据 / L2 ----------------

