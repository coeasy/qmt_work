from app.routes._common import ok, err, state, settings, Request

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

@router.get("/signal/mode")
async def get_signal_mode():
    if state.signal_router is None:
        return err(503, "信号路由未初始化")
    return ok({"mode": state.signal_router.mode})

@router.post("/signal/mode")
async def set_signal_mode(body: dict):
    if state.signal_router is None:
        return err(503, "信号路由未初始化")
    try:
        mode = state.signal_router.set_mode(body.get("mode", "live"))
    except ValueError as exc:
        return err(400, str(exc))
    state.db.audit("admin", "signal.mode", "global", {"mode": mode}, "ok")
    return ok({"mode": mode})

@router.post("/signal/submit")
async def signal_submit(body: dict):
    if state.signal_router is None:
        return err(503, "信号路由未初始化")
    from gateway.signal_router import Signal
    sig = Signal(
        source=body.get("source", "manual"),
        code=body.get("code", ""),
        side=body.get("side", "buy"),
        volume=int(body.get("volume", 0)),
        price=float(body.get("price", 0) or 0),
        price_type=body.get("price_type", "limit"),
        remark=body.get("remark", ""),
        broker_id=body.get("broker_id", ""),
        payload=body.get("payload", {}))
    res = await state.signal_router.route(sig)
    if isinstance(res, dict) and res.get("ok"):
        return ok(res)
    return err(503, res.get("reason", "信号路由失败") if isinstance(res, dict) else "信号路由失败")

@router.post("/signal/confirm")
async def signal_confirm(body: dict):
    """二次确认：携带 confirm_token（及 TOTP 码）执行挂起的大额下单。"""
    if state.signal_router is None:
        return err(503, "信号路由未初始化")
    token = body.get("confirm_token", "")
    if not token:
        return err(400, "缺少 confirm_token")
    res = await state.signal_router.confirm(token, body.get("totp_code", ""))
    if res.get("ok"):
        return ok(res)
    return err(400, res.get("reason", "确认失败"), res)

@router.post("/signal/webhook")
async def signal_webhook(request: Request):
    """外部策略系统信号入站：HMAC-SHA256 签名校验（QMT_WEBHOOK_SECRET 非空时），经统一信号路由。"""
    if state.signal_router is None:
        return err(503, "信号路由未初始化")
    import hashlib
    import hmac
    import json
    secret = settings.webhook_secret
    raw = await request.body()
    if secret:
        provided = request.headers.get("x-signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            return err(401, "签名校验失败")
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return err(400, "invalid json body")
    from gateway.signal_router import Signal
    sig = Signal(
        source=body.get("source", "webhook"),
        code=body.get("code", ""), side=body.get("side", "buy"),
        volume=int(body.get("volume", 0)), price=float(body.get("price", 0) or 0),
        price_type=body.get("price_type", "limit"), remark=body.get("remark", ""),
        broker_id=body.get("broker_id", ""), payload=body.get("payload", {}))
    res = await state.signal_router.route(sig)
    state.db.audit("webhook", "signal.submit", body.get("code", ""), body, "ok")
    if isinstance(res, dict) and res.get("ok"):
        return ok(res)
    reason = res.get("reason", "信号路由失败") if isinstance(res, dict) else "信号路由失败"
    return err(400, reason, res)


# ---------------- 目标持仓差量同步 ----------------

