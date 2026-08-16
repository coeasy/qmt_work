from app.routes._common import ok, err, state

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import time



router = APIRouter()

@router.get("/alerts/rules")
async def list_alert_rules():
    if state.db is None:
        return err(503, "数据库未初始化")
    rows = state.db.query("SELECT * FROM alert_rules ORDER BY id")
    return ok(rows)

@router.post("/alerts/rules")
async def save_alert_rule(body: dict):
    if state.db is None:
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
        state.db.execute(f"UPDATE alert_rules SET {','.join(fields)} WHERE id=?",
                         (*payload.values(), int(body["id"])))
        rid = int(body["id"])
    else:
        rid = state.db.insert("alert_rules", payload)
    return ok({"id": rid})

@router.delete("/alerts/rules/{rid}")
async def delete_alert_rule(rid: int):
    state.db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
    return ok({"deleted": True})

@router.post("/alerts/test")
async def test_alert(body: dict):
    if state.alert_engine is None:
        return err(503, "告警引擎未初始化")
    event = body.get("event", "system.test")
    state.alert_engine.evaluate_event(event, body.get("payload", {}))
    return ok({"fired": True, "event": event})

@router.get("/alerts/history")
async def alert_history(limit: int = 50):
    if state.db is None:
        return err(503, "数据库未初始化")
    rows = state.db.query("SELECT * FROM alerts_history ORDER BY id DESC LIMIT ?", (limit,))
    return ok(rows)


# ---------------- 委托对账核销（A2） ----------------

