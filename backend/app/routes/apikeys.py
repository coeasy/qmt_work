from app.routes._common import ok, err, state, uuid

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import base64
import datetime
import hashlib
import io
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional



router = APIRouter()

@router.get("/api-keys")
async def list_api_keys():
    from gateway.apikey import ApiKeyStore
    rows = state.db.query(
        "SELECT id, name, scopes, rate_limit, status, created_at, "
        "ip_allow, expires_at, grace_until, "
        "substr(key_hash,1,8) AS key_prefix FROM api_keys ORDER BY id")
    for r in rows:
        if ApiKeyStore._is_expired(r):
            r["status"] = "expired"
    return ok(rows)

@router.post("/api-keys")
async def create_api_key(body: dict):
    import hashlib
    raw = f"qmt-{uuid.uuid4().hex[:24]}"
    kid = state.db.insert("api_keys", {
        "key_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "user_id": 1, "name": body.get("name", "default"),
        "scopes": body.get("scopes", "market,trade,account,backtest"),
        "rate_limit": int(body.get("rate_limit", 0)), "status": "active",
        "ip_allow": body.get("ip_allow", ""),
        "expires_at": body.get("expires_at", ""),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if state.apikey_store:
        state.apikey_store.invalidate()
    state.db.audit("admin", "api_key.create", f"#{kid}",
                   {"name": body.get("name"), "scopes": body.get("scopes"),
                    "ip_allow": body.get("ip_allow", ""),
                    "expires_at": body.get("expires_at", "")}, "ok")
    return ok({"id": kid, "api_key": raw, "name": body.get("name", "default"),
               "scopes": body.get("scopes", "market,trade,account,backtest"),
               "rate_limit": int(body.get("rate_limit", 0)),
               "ip_allow": body.get("ip_allow", ""),
               "expires_at": body.get("expires_at", "")})

@router.patch("/api-keys/{kid}")
async def update_api_key(kid: int, body: dict):
    row = state.db.query_one("SELECT id FROM api_keys WHERE id=?", (kid,))
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
    state.db.execute(f"UPDATE api_keys SET {','.join(fields)} WHERE id=?", tuple(vals))
    if state.apikey_store:
        state.apikey_store.invalidate()
    state.db.audit("admin", "api_key.update", f"#{kid}", body, "ok")
    return ok({"updated": True})

@router.delete("/api-keys/{kid}")
async def delete_api_key(kid: int):
    state.db.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    if state.apikey_store:
        state.apikey_store.invalidate()
    state.db.audit("admin", "api_key.delete", f"#{kid}", {}, "ok")
    return ok({"deleted": True})

@router.post("/api-keys/{kid}/rotate")
async def rotate_api_key(kid: int):
    """轮换密钥：生成新密钥立即生效，旧密钥立即失效；grace_until 记录宽限标记（7天）。"""
    import hashlib
    from datetime import datetime, timedelta
    row = state.db.query_one("SELECT id FROM api_keys WHERE id=?", (kid,))
    if not row:
        return err(404, "密钥不存在")
    raw = f"qmt-{uuid.uuid4().hex[:24]}"
    grace = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    state.db.execute(
        "UPDATE api_keys SET key_hash=?, grace_until=?, created_at=? WHERE id=?",
        (hashlib.sha256(raw.encode()).hexdigest(), grace,
         time.strftime("%Y-%m-%dT%H:%M:%S"), kid))
    if state.apikey_store:
        state.apikey_store.invalidate()
    state.db.audit("admin", "api_key.rotate", f"#{kid}", {"grace_until": grace}, "ok")
    return ok({"id": kid, "api_key": raw, "grace_until": grace,
               "note": "旧密钥已立即失效；grace_until 为轮换宽限标记"})


# ---------------- 通知配置 ----------------

