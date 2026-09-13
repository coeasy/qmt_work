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
    from gateway.notifier import NotifyMessage, _render
    event = body.get("event", "system.test")
    title = body.get("title", "测试通知")
    text = body.get("body", "来自 qmt_work 的通知测试。")
    # ★ 阶段 3 实测缺陷（2026-09-13）：此处原写 `ctx = {...}` 覆盖了路由入参
    #   ctx（AppContext）→ 下一行 `ctx.notifier` 变成访问 dict 属性，
    #   端点恒定 500（前端「发送测试通知」永远失败且原因是 AttributeError）。
    #   模板上下文改名 tpl_ctx，路由上下文 ctx 保持不动。
    tpl_ctx = {"event": event, "title": title, "body": text,
               "payload": body.get("payload", {}), "ts": "", "name": cfg.get("name", "")}
    rendered = _render(cfg.get("template", "{{title}}\n{{body}}"), tpl_ctx)
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

