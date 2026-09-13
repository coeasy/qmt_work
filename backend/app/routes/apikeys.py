from core.context import AppContext, get_ctx
# --- stdlib imports injected by fix_route_imports ---
import hashlib
import time
import uuid

from fastapi import APIRouter, Depends

from app.routes._common import err, ok

router = APIRouter()

@router.get("/api-keys")
async def list_api_keys(ctx: AppContext = Depends(get_ctx)):
    """获取api-keys（GET /api-keys）。"""
    from gateway.apikey import ApiKeyStore
    rows = ctx.db.query(
        "SELECT id, name, scopes, rate_limit, status, created_at, "
        "ip_allow, expires_at, grace_until, last_used_at, use_count, "
        "substr(key_hash,1,8) AS key_prefix FROM api_keys ORDER BY id")
    for r in rows:
        if ApiKeyStore._is_expired(r):
            r["status"] = "expired"
    return ok(rows)

@router.post("/api-keys")
async def create_api_key(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交api-keys（POST /api-keys）。"""
    raw = f"qmt-{uuid.uuid4().hex[:24]}"
    kid = ctx.db.insert("api_keys", {
        "key_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "user_id": 1, "name": body.get("name", "default"),
        "scopes": body.get("scopes", "market,trade,account,backtest"),
        "rate_limit": int(body.get("rate_limit", 0)), "status": "active",
        "ip_allow": body.get("ip_allow", ""),
        "expires_at": body.get("expires_at", ""),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if ctx.apikey_store:
        ctx.apikey_store.invalidate()
    ctx.db.audit("admin", "api_key.create", f"#{kid}",
                   {"name": body.get("name"), "scopes": body.get("scopes"),
                    "ip_allow": body.get("ip_allow", ""),
                    "expires_at": body.get("expires_at", "")}, "ok")
    return ok({"id": kid, "api_key": raw, "name": body.get("name", "default"),
               "scopes": body.get("scopes", "market,trade,account,backtest"),
               "rate_limit": int(body.get("rate_limit", 0)),
               "ip_allow": body.get("ip_allow", ""),
               "expires_at": body.get("expires_at", "")})

@router.patch("/api-keys/{kid}")
async def update_api_key(kid: int, body: dict, ctx: AppContext = Depends(get_ctx)):
    """更新api-keys（PATCH /api-keys/{kid}）。"""
    row = ctx.db.query_one("SELECT id FROM api_keys WHERE id=?", (kid,))
    if not row:
        return err(404, "密钥不存在")
    fields, vals = [], []
    for k in ("name", "scopes", "rate_limit", "status", "ip_allow", "expires_at"):
        if k in body:
            fields.append(f"{k}=?")
            vals.append(int(body[k]) if k == "rate_limit" else body[k])
    if not fields:
        return err(400, "无更新字段")
    vals.append(kid)
    ctx.db.execute(f"UPDATE api_keys SET {','.join(fields)} WHERE id=?", tuple(vals))
    if ctx.apikey_store:
        ctx.apikey_store.invalidate()
    ctx.db.audit("admin", "api_key.update", f"#{kid}", body, "ok")
    return ok({"updated": True})

@router.delete("/api-keys/{kid}")
async def delete_api_key(kid: int, ctx: AppContext = Depends(get_ctx)):
    """删除api-keys（DELETE /api-keys/{kid}）。"""
    ctx.db.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    if ctx.apikey_store:
        ctx.apikey_store.invalidate()
    ctx.db.audit("admin", "api_key.delete", f"#{kid}", {}, "ok")
    return ok({"deleted": True})

@router.post("/api-keys/batch-delete")
async def batch_delete_api_keys(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交api-keys / batch-delete（POST /api-keys/batch-delete）。"""
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return err(400, "ids 不能为空")
    for kid in ids:
        ctx.db.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    if ctx.apikey_store:
        ctx.apikey_store.invalidate()
    ctx.db.audit("admin", "api_key.batch_delete", f"#{len(ids)}", {"ids": ids}, "ok")
    return ok({"deleted": len(ids)})

@router.post("/api-keys/{kid}/rotate")
async def rotate_api_key(kid: int, ctx: AppContext = Depends(get_ctx)):
    """轮换密钥：生成新密钥立即生效，旧密钥立即失效；grace_until 记录宽限标记（7天）。"""
    from datetime import datetime, timedelta
    row = ctx.db.query_one("SELECT id FROM api_keys WHERE id=?", (kid,))
    if not row:
        return err(404, "密钥不存在")
    raw = f"qmt-{uuid.uuid4().hex[:24]}"
    grace = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    ctx.db.execute(
        "UPDATE api_keys SET key_hash=?, grace_until=?, created_at=? WHERE id=?",
        (hashlib.sha256(raw.encode()).hexdigest(), grace,
         time.strftime("%Y-%m-%dT%H:%M:%S"), kid))
    if ctx.apikey_store:
        ctx.apikey_store.invalidate()
    ctx.db.audit("admin", "api_key.rotate", f"#{kid}", {"grace_until": grace}, "ok")
    return ok({"id": kid, "api_key": raw, "grace_until": grace,
               "note": "旧密钥已立即失效；grace_until 为轮换宽限标记"})


@router.post("/api-keys/clean-unused")
async def clean_unused_api_keys(body: dict, ctx: AppContext = Depends(get_ctx)):
    """清理无效密钥：删除超过 X 天未使用的密钥，默认 30 天。"""
    days = int(body.get("days") or 30)
    if days < 1:
        return err(400, "days 至少为 1")
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    # 注意：last_used_at 为空视为"从未使用"，也满足"从未使用>days天"条件
    # 保留 active 且：(last_used_at 为空且 created_at < cutoff) OR last_used_at < cutoff
    deleted = 0
    rows = ctx.db.query(
        "SELECT id, last_used_at, created_at FROM api_keys "
        "WHERE status='active' AND (last_used_at < ? OR last_used_at = '' OR last_used_at IS NULL)",
        (cutoff,))
    ids_to_del = [r["id"] for r in rows]
    if ids_to_del:
        place = ",".join("?" * len(ids_to_del))
        deleted = len(ids_to_del)
        ctx.db.execute(f"DELETE FROM api_keys WHERE id IN ({place})", tuple(ids_to_del))
    if ctx.apikey_store:
        ctx.apikey_store.invalidate()
    ctx.db.audit("admin", "api_key.clean", "", {"days": days, "deleted": deleted}, "ok")
    return ok({"deleted": deleted, "cutoff": cutoff})


# ---------------- 通知配置 ----------------

