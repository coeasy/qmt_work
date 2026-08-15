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

@router.post("/strategies/generate")
async def strategies_generate(body: dict):
    from tools.strategy_gen import generate_strategy
    try:
        return ok(generate_strategy(
            body.get("strategy_type") or body.get("strategy", ""), body.get("code", "600519.SH"),
            body.get("client_path", ""), body.get("account_id", ""),
            body.get("params") or {}))
    except ValueError as exc:
        return err(400, str(exc))

@router.post("/strategies/save")
async def strategies_save(body: dict):
    from tools.strategy_gen import save_qmt_strategy
    try:
        return ok(save_qmt_strategy(body.get("filename", "strategy.py"),
                                    body.get("content", ""),
                                    body.get("client_path", "")))
    except OSError as exc:
        return err(400, f"写入失败：{exc}")


# ---------------- 手动交易（Trade 页） ----------------

