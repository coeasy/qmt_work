from fastapi import APIRouter

from app.routes._common import err, ok, state

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/webhooks")
async def list_webhooks():
    """获取webhooks（GET /webhooks）。"""
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    return ok(state.webhook_out.list_subs())

@router.post("/webhooks")
async def save_webhook(body: dict):
    """创建/提交webhooks（POST /webhooks）。"""
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    try:
        sid = state.webhook_out.save_sub(body)
    except ValueError as exc:
        return err(400, str(exc))
    state.db.audit("admin", "webhook.save", f"#{sid}",
                   {"name": body.get("name"), "url": body.get("url"),
                    "events": body.get("events", "*")}, "ok")
    return ok({"id": sid})

@router.delete("/webhooks/{sid}")
async def delete_webhook(sid: int):
    """删除webhooks（DELETE /webhooks/{sid}）。"""
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    state.webhook_out.delete_sub(sid)
    state.db.audit("admin", "webhook.delete", f"#{sid}", {}, "ok")
    return ok({"deleted": True})

@router.post("/webhooks/batch-delete")
async def batch_delete_webhooks(body: dict):
    """创建/提交webhooks / batch-delete（POST /webhooks/batch-delete）。"""
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    for sid in ids:
        state.webhook_out.delete_sub(sid)
        state.db.audit("admin", "webhook.delete", f"#{sid}", {}, "ok")
    return ok({"deleted": len(ids)})

@router.post("/webhooks/{sid}/test")
async def test_webhook(sid: int):
    """创建/提交webhooks / test（POST /webhooks/{sid}/test）。"""
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    try:
        return ok(await state.webhook_out.test(sid))
    except KeyError as exc:
        return err(404, str(exc))

@router.get("/webhooks/deliveries")
async def webhook_deliveries(sid: int = 0, limit: int = 50):
    """获取webhooks / deliveries（GET /webhooks/deliveries）。"""
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    return ok(state.webhook_out.deliveries(sid=sid, limit=limit))


# ---------------- 告警规则引擎 ----------------

