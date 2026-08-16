from app.routes._common import ok, err, state, crypto

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import time



router = APIRouter()

@router.get("/config/llm")
async def get_llm_config():
    row = state.db.query_one("SELECT * FROM llm_config WHERE scope='global' ORDER BY id LIMIT 1")
    if not row:
        return ok({"provider": "openai", "base_url": "", "api_key_masked": "",
                   "model": "", "temperature": 0.2, "configured": False})
    return ok({"provider": row["provider"], "base_url": row["base_url"],
               "api_key_masked": crypto.mask_secret(crypto.decrypt_plain(row["api_key_enc"])) if row["api_key_enc"] else "",
               "model": row["model"], "temperature": row["temperature"], "configured": True})

@router.put("/config/llm")
async def put_llm_config(body: dict):
    if not body.get("base_url") or not body.get("model"):
        return err(400, "base_url 与 model 必填")
    api_key_enc = crypto.encrypt_plain(body.get("api_key", "")) if body.get("api_key") else ""
    existing = state.db.query_one("SELECT id FROM llm_config WHERE scope='global' ORDER BY id LIMIT 1")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if existing:
        if api_key_enc:
            state.db.execute(
                "UPDATE llm_config SET provider=?, base_url=?, model=?, temperature=?, "
                "api_key_enc=?, updated_at=? WHERE id=?",
                (body.get("provider", "openai"), body["base_url"], body["model"],
                 float(body.get("temperature", 0.2)), api_key_enc, now, existing["id"]))
        else:
            state.db.execute(
                "UPDATE llm_config SET provider=?, base_url=?, model=?, temperature=?, updated_at=? WHERE id=?",
                (body.get("provider", "openai"), body["base_url"], body["model"],
                 float(body.get("temperature", 0.2)), now, existing["id"]))
    else:
        state.db.insert("llm_config", {
            "scope": "global", "provider": body.get("provider", "openai"),
            "base_url": body["base_url"], "api_key_enc": api_key_enc,
            "model": body["model"], "temperature": float(body.get("temperature", 0.2)),
            "timeout_ms": 60000, "is_default": 1, "updated_at": now})
    return ok({"saved": True})


# ---------------- 运行时配置中心（引擎级参数热更新，配置灵活化） ----------------

@router.get("/config/runtime")
async def get_runtime_config():
    """读取全部运行时引擎参数（含生效值/默认值/说明；修改后立即生效）。"""
    if state.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    return ok(state.runtime_config.all())

@router.put("/config/runtime")
async def put_runtime_config(body: dict):
    """批量更新引擎运行参数（校验类型与下限，热更新无需重启）。"""
    rc = state.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    try:
        changed = rc.set_many(body)
    except ValueError as exc:
        return err(400, str(exc))
    state.db.audit("admin", "runtime_config.update", "global",
                   {"changed": changed}, "ok")
    return ok({"saved": True, "changed": changed, "config": rc.all()})

@router.post("/config/runtime/reset")
async def reset_runtime_config(body: dict | None = None):
    """恢复默认：body.key 指定单个（缺省全部重置）。"""
    rc = state.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    key = (body or {}).get("key", "")
    changed = rc.reset(key=key or "")
    state.db.audit("admin", "runtime_config.reset", key or "*",
                   {"changed": changed}, "ok")
    return ok({"reset": changed, "config": rc.all()})

@router.get("/config/runtime/history")
async def get_runtime_config_history(limit: int = 50):
    """变更历史（含回滚入口），按时间倒序。"""
    if state.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    try:
        limit = max(1, min(int(limit), 200))
    except (ValueError, TypeError):
        limit = 50
    rows = state.runtime_config.history(limit)
    return ok({"rows": rows})

@router.post("/config/runtime/rollback")
async def rollback_runtime_config(body: dict):
    """回滚到指定历史记录 id：将该记录的旧值重新写回。"""
    if state.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    entry_id = int((body or {}).get("id", 0))
    if not entry_id:
        return err(400, "缺少 id")
    ok_flag = state.runtime_config.rollback(entry_id)
    if not ok_flag:
        return err(400, "回滚失败：记录不存在或 key 非法")
    state.db.audit("admin", "runtime_config.rollback", str(entry_id),
                   {"id": entry_id}, "ok")
    return ok({"rolled_back": entry_id, "config": state.runtime_config.all()})


# ---------------- 风控配置（运行期可调，持久化 risk_config） ----------------

@router.get("/config/risk")
async def get_risk_config():
    """读取风控参数（含日级限额与熔断实时状态）。"""
    rm = state.risk
    if rm is None:
        return ok({
            "max_amount": 100_000.0, "min_qty": 100, "max_position_ratio": 0.3,
            "max_single_position_ratio": 0.2, "max_orders_per_min": 30,
            "daily_amount_limit": 0.0, "daily_loss_limit": 0.0,
            "per_code_daily_orders": 0})
    data = rm.to_dict()
    data["daily"] = rm.daily_stats()
    return ok(data)

@router.put("/config/risk")
async def put_risk_config(body: dict):
    """更新风控参数（持久化到 risk_config 表）。"""
    rm = state.risk
    if rm is None:
        return err(503, "风控未初始化")
    try:
        changed = rm.update_from(body)
    except ValueError as exc:
        return err(400, str(exc))
    rm.save_to_db(state.db)
    state.db.audit("admin", "risk_config.update", "global",
                   {"changed": changed}, "ok")
    return ok({"saved": True, "changed": changed, "config": rm.to_dict()})

@router.get("/config/risk/daily")
async def get_risk_daily():
    """日级风控实时用量与熔断状态（B4）。"""
    if state.risk is None:
        return err(503, "风控未初始化")
    return ok(state.risk.daily_stats())

@router.post("/config/risk/circuit")
async def post_risk_circuit(body: dict | None = None):
    """熔断开关：action=trip 手动熔断（停止买入开仓）/ action=reset 解除熔断。"""
    rm = state.risk
    if rm is None:
        return err(503, "风控未初始化")
    body = body or {}
    action = str(body.get("action", "reset")).lower()
    if action == "trip":
        reason = str(body.get("reason") or "人工熔断：暂停一切买入开仓")
        rm.trip(reason)
        if state.notifier:
            await state.notifier.notify("risk.circuit", "风控熔断已开启", reason,
                                        {"reason": reason, "manual": True})
    elif action == "reset":
        rm.reset_circuit()
        if state.notifier:
            await state.notifier.notify("risk.circuit", "风控熔断已解除",
                                        "已恢复买入开仓，日初净值重新锚定", {"manual": True})
    else:
        return err(400, "action 仅支持 trip / reset")
    state.db.audit("admin", f"risk.circuit.{action}", "global",
                   {"body": body}, "ok")
    return ok(rm.daily_stats())


# ---------------- 健康检查 ----------------

