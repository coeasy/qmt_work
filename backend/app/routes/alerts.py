from core.context import AppContext, get_ctx
# --- stdlib imports injected by fix_route_imports ---
import time

from fastapi import APIRouter, Depends

from app.routes._common import audit_log, err, ok

router = APIRouter()

@router.get("/alerts/rules")
async def list_alert_rules(ctx: AppContext = Depends(get_ctx)):
    """获取alerts / rules（GET /alerts/rules）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    rows = ctx.db.query("SELECT * FROM alert_rules ORDER BY id")
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
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if body.get("id"):
        fields = [f"{k}=?" for k in payload]
        ctx.db.execute(f"UPDATE alert_rules SET {','.join(fields)} WHERE id=?",
                         (*payload.values(), int(body["id"])))
        rid = int(body["id"])
    else:
        rid = ctx.db.insert("alert_rules", payload)
    audit_log("api", "save_alert_rule", body.get('name',''), body)

    return ok({"id": rid})

@router.delete("/alerts/rules/{rid}")
async def delete_alert_rule(rid: int, ctx: AppContext = Depends(get_ctx)):
    """删除alerts / rules（DELETE /alerts/rules/{rid}）。"""
    ctx.db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
    return ok({"deleted": True})

@router.post("/alerts/rules/batch-delete")
async def batch_delete_alert_rules(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交alerts / rules / batch-delete（POST /alerts/rules/batch-delete）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    ctx.db.execute(
        "DELETE FROM alert_rules WHERE id IN ({})".format(",".join("?" * len(ids))), ids)
    return ok({"deleted": len(ids)})

@router.post("/alerts/test")
async def test_alert(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交alerts / test（POST /alerts/test）。"""
    if ctx.alert_engine is None:
        return err(503, "告警引擎未初始化")
    event = body.get("event", "system.test")
    ctx.alert_engine.evaluate_event(event, body.get("payload", {}))
    return ok({"fired": True, "event": event})

@router.get("/alerts/history")
async def alert_history(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """获取alerts / history（GET /alerts/history）。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    rows = ctx.db.query("SELECT * FROM alerts_history ORDER BY id DESC LIMIT ?", (limit,))
    return ok(rows)


# ---------------- 委托对账核销（A2） ----------------

