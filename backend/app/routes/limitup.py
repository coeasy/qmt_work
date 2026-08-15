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

@router.get("/limitup/status")
async def limitup_status():
    m = state.limitup_monitor
    return ok(m.status() if m else {"running": False, "pool": []})

@router.post("/limitup/pool")
async def limitup_pool_add(body: dict):
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    try:
        return ok(m.add(body.get("code", "")))
    except ValueError as exc:
        return err(400, str(exc))

@router.delete("/limitup/pool")
async def limitup_pool_remove(code: str):
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    m.remove(code)
    return ok({"removed": code})

@router.post("/limitup/start")
async def limitup_start(body: dict):
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    try:
        return ok(await m.start({
            "limit_pct": float(body.get("limit_pct", 0.1)),
            "cutoff": str(body.get("cutoff", "10:00")),
            "min_rise": float(body.get("min_rise", 0.03)),
            "buy_volume": int(body.get("buy_volume", 0)),
            "do_trade": bool(body.get("do_trade", False)),
            "interval": float(body.get("interval", 2.0))}))
    except ValueError as exc:
        return err(400, str(exc))

@router.post("/limitup/stop")
async def limitup_stop():
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    return ok(await m.stop())

@router.post("/limitup/reset")
async def limitup_reset():
    m = state.limitup_monitor
    if m is None:
        return err(503, "涨停监控未初始化")
    m.reset_triggered()
    return ok({"reset": True})


# ---------------- 算法单（TWAP/VWAP） ----------------

