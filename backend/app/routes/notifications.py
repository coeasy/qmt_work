from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import audit_log, err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/notifications")
async def list_notifications(ctx: AppContext = Depends(get_ctx)):
    """获取notifications（GET /notifications）。"""
    if ctx.notifier is None:
        return err(503, "通知中心未初始化")
    return ok(ctx.notifier.list_configs())

@router.post("/notifications")
async def save_notification(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交notifications（POST /notifications）。"""
    if ctx.notifier is None:
        return err(503, "通知中心未初始化")
    nid = ctx.notifier.save_config(body)
    audit_log("api", "save_notification", body.get('name',''), body)

    return ok({"id": nid})

@router.delete("/notifications/{nid}")
async def delete_notification(nid: int, ctx: AppContext = Depends(get_ctx)):
    """删除notifications（DELETE /notifications/{nid}）。"""
    if ctx.notifier is None:
        return err(503, "通知中心未初始化")
    ctx.notifier.delete_config(nid)
    return ok({"deleted": True})

@router.post("/notifications/batch-delete")
async def batch_delete_notifications(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交notifications / batch-delete（POST /notifications/batch-delete）。"""
    if ctx.notifier is None:
        return err(503, "通知中心未初始化")
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    for nid in ids:
        ctx.notifier.delete_config(nid)
    return ok({"deleted": len(ids)})

@router.post("/notifications/test")
async def test_notification(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交notifications / test（POST /notifications/test）。"""
    if ctx.notifier is None:
        return err(503, "通知中心未初始化")
    cfg = body.get("config", {})
    # 临时构造 Notifier 子任务，复用同一个 http client
    from gateway.notifier import _render
    event = body.get("event", "system.test")
    title = body.get("title", "测试通知")
    text = body.get("body", "来自 qmt_work 的通知测试。")
    ctx = {"event": event, "title": title, "body": text, "payload": body.get("payload", {}), "ts": "", "name": cfg.get("name", "")}
    rendered = _render(cfg.get("template", "{{title}}\n{{body}}"), ctx)
    # 直接复用 Notifier 内部 _send_one 逻辑：构造一条伪配置
    from gateway.notifier import NotifyMessage
    await ctx.notifier._send_one({
        "id": 0, "name": cfg.get("name", "test"), "channel": cfg.get("channel", "webhook"),
        "params": cfg.get("params", {}), "template": cfg.get("template", "{{title}}\n{{body}}"),
        "template_body": "{{body}}", "enabled": 1, "events": "*"},
        NotifyMessage(event=event, title=title, body=text, payload=body.get("payload", {})))
    return ok({"sent": True, "preview": rendered})

@router.get("/notifications/logs")
async def notification_logs(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """获取notifications / logs（GET /notifications/logs）。"""
    if ctx.notifier is None:
        return err(503, "通知中心未初始化")
    return ok(ctx.notifier.recent_logs(limit))


# ---------------- 出站 webhook 订阅（B2：事件投递给外部服务） ----------------

