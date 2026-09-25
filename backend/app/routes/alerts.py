from core.context import AppContext, get_ctx
# --- stdlib imports injected by fix_route_imports ---

from fastapi import APIRouter, Depends

from app.routes._common import audit_log, err, ok
from app.services import alerts_store
from core.clock import now_iso

router = APIRouter()

@router.get("/alerts/rules")
async def list_alert_rules(ctx: AppContext = Depends(get_ctx)):
    """获取alerts / rules（GET /alerts/rules）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    rows = alerts_store.list_rules(ctx.db)
    return ok(rows)

@router.post("/alerts/rules")
async def save_alert_rule(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交alerts / rules（POST /alerts/rules）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    payload = {
        "name": body.get("name", ""),
        "enabled": 1 if body.get("enabled", True) else 0,
        "event": body.get("event", "*"),
        "metric": body.get("metric", ""),
        "op": body.get("op", ">"),
        "threshold": float(body.get("threshold", 0) or 0),
        "channel": body.get("channel", "*"),
        "cooldown_seconds": int(body.get("cooldown_seconds", 300)),
        "created_at": now_iso(),
    }
    rid = alerts_store.save_rule(
        ctx.db, payload, int(body["id"]) if body.get("id") else None)
    audit_log("api", "save_alert_rule", body.get('name',''), body)

    return ok({"id": rid})

@router.delete("/alerts/rules/{rid}")
async def delete_alert_rule(rid: int, ctx: AppContext = Depends(get_ctx)):
    """删除alerts / rules（DELETE /alerts/rules/{rid}）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    alerts_store.delete_rule(ctx.db, rid)
    return ok({"deleted": True})

@router.post("/alerts/rules/batch-delete")
async def batch_delete_alert_rules(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交alerts / rules / batch-delete（POST /alerts/rules/batch-delete）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    alerts_store.delete_rules(ctx.db, ids)
    return ok({"deleted": len(ids)})

@router.post("/alerts/test")
async def test_alert(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交alerts / test（POST /alerts/test）。

    ★ 必须如实回报「到底有没有规则被触发」。
    曾无条件返回 `{"fired": True}`，而 `evaluate_event` 实际返回 None ——
    一条规则都没命中时也显示「已触发」。用户据此以为告警链路是通的，
    等真出事时才发现根本没配规则。这是**假成功**，比报错危险。
    """
    if ctx.alert_engine is None:
        return err(503, "告警引擎未初始化")
    event = body.get("event", "system.test")
    payload = body.get("payload", {})
    fired = ctx.alert_engine.evaluate_event(event, payload)
    # 各实现的返回值形态不一（None / list / dict），统一成「数量 + 明细」
    if fired is None:
        items: list = []
    elif isinstance(fired, list):
        items = fired
    elif isinstance(fired, dict):
        items = [fired]
    else:
        items = [{"raw": fired}]
    return ok({
        "fired": bool(items),
        "count": len(items),
        "event": event,
        "items": items,
    })

@router.get("/alerts/history")
async def alert_history(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """获取alerts / history（GET /alerts/history）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    rows = alerts_store.list_history(ctx.db, limit)
    return ok(rows)


# ---------------- 委托对账核销（A2） ----------------

