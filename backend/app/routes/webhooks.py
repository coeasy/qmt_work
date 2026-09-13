from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/webhooks")
async def list_webhooks(ctx: AppContext = Depends(get_ctx)):
    """获取webhooks（GET /webhooks）。"""
    if ctx.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    return ok(ctx.webhook_out.list_subs())

@router.post("/webhooks")
async def save_webhook(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交webhooks（POST /webhooks）。"""
    if ctx.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    try:
        sid = ctx.webhook_out.save_sub(body)
    except ValueError as exc:
        return err(400, str(exc))
    ctx.db.audit("admin", "webhook.save", f"#{sid}",
                   {"name": body.get("name"), "url": body.get("url"),
                    "events": body.get("events", "*")}, "ok")
    return ok({"id": sid})

@router.delete("/webhooks/{sid}")
async def delete_webhook(sid: int, ctx: AppContext = Depends(get_ctx)):
    """删除webhooks（DELETE /webhooks/{sid}）。"""
    if ctx.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    ctx.webhook_out.delete_sub(sid)
    ctx.db.audit("admin", "webhook.delete", f"#{sid}", {}, "ok")
    return ok({"deleted": True})

@router.post("/webhooks/batch-delete")
async def batch_delete_webhooks(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交webhooks / batch-delete（POST /webhooks/batch-delete）。"""
    if ctx.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    for sid in ids:
        ctx.webhook_out.delete_sub(sid)
        ctx.db.audit("admin", "webhook.delete", f"#{sid}", {}, "ok")
    return ok({"deleted": len(ids)})

@router.post("/webhooks/{sid}/test")
async def test_webhook(sid: int, ctx: AppContext = Depends(get_ctx)):
    """创建/提交webhooks / test（POST /webhooks/{sid}/test）。"""
    if ctx.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    try:
        return ok(await ctx.webhook_out.test(sid))
    except KeyError as exc:
        return err(404, str(exc))

@router.get("/webhooks/deliveries")
async def webhook_deliveries(sid: int = 0, limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """获取webhooks / deliveries（GET /webhooks/deliveries）。"""
    if ctx.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    return ok(ctx.webhook_out.deliveries(sid=sid, limit=limit))


# ---------------- 告警规则引擎 ----------------

