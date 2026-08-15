from app.routes._common import ok, err, state

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

@router.get("/webhooks")
async def list_webhooks():
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    return ok(state.webhook_out.list_subs())

@router.post("/webhooks")
async def save_webhook(body: dict):
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    try:
        sid = state.webhook_out.save_sub(body)
    except ValueError as exc:
        return err(400, str(exc))
    state.db.audit("admin", "webhook.save", f"#{sid}",
                   {"name": body.get("name"), "url": body.get("url"),
                    "events": body.get("events", "*")}, "ok")
    return ok({"id": sid})

@router.delete("/webhooks/{sid}")
async def delete_webhook(sid: int):
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    state.webhook_out.delete_sub(sid)
    state.db.audit("admin", "webhook.delete", f"#{sid}", {}, "ok")
    return ok({"deleted": True})

@router.post("/webhooks/{sid}/test")
async def test_webhook(sid: int):
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    try:
        return ok(await state.webhook_out.test(sid))
    except KeyError as exc:
        return err(404, str(exc))

@router.get("/webhooks/deliveries")
async def webhook_deliveries(sid: int = 0, limit: int = 50):
    if state.webhook_out is None:
        return err(503, "出站 webhook 未初始化")
    return ok(state.webhook_out.deliveries(sid=sid, limit=limit))


# ---------------- 告警规则引擎 ----------------

