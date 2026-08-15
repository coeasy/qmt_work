from app.routes._common import ok, err, state, _need, _call

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

@router.get("/reference/calendar")
async def reference_calendar(start: str = "", end: str = ""):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_trading_calendar, start, end)

@router.get("/reference/sectors")
async def reference_sectors():
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_sector_list)

@router.get("/reference/sector-stocks")
async def reference_sector_stocks(sector: str = "沪深A股"):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_sector_stocks, sector)

@router.get("/reference/financial")
async def reference_financial(code: str):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_financial, code)

