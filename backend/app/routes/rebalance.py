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

@router.post("/rebalance")
async def rebalance(body: dict):
    """等权篮子再平衡：targets=[{code,target_ratio}] -> 调仓单（阈值过滤+拆单+涨跌停处理）。

    do_trade=True 时经券商真实下单；否则仅生成计划。
    """
    targets = body.get("targets", [])
    if not targets:
        return err(400, "targets 不能为空")
    b = _need(body.get("conn_id") or None)
    if b is None:
        return err(503, "未连接任何券商客户端。")
    cash = await _call(b, b.gateway.get_cash)
    if isinstance(cash, dict) and cash.get("code"):
        return cash
    pos = await _call(b, b.gateway.get_positions)
    if isinstance(pos, dict) and pos.get("code"):
        return pos
    total = (cash.get("assets", 0.0) or 0.0) + sum(p.get("market_value", 0.0) for p in pos)
    current = {p["code"]: p.get("market_value", 0.0) for p in pos}
    delta_min = float(body.get("delta_min", 3000))
    delta_max = float(body.get("delta_max", 30000))
    do_trade = bool(body.get("do_trade", False))
    orders = []
    for t in targets:
        code = t["code"]
        target_val = total * float(t.get("target_ratio", 0))
        diff = round(target_val - current.get(code, 0.0), 2)
        if abs(diff) < delta_min:
            continue
        quote = await _call(b, b.gateway.get_quote, code)
        if isinstance(quote, dict) and quote.get("code") and "last" not in quote:
            return quote
        last = quote.get("last")
        high = quote.get("high"); low = quote.get("low")
        if last is None:
            continue
        if (diff > 0 and last >= (high or 1e9)) or (diff < 0 and last <= (low or 0)):
            orders.append({"code": code, "skipped": "limit", "diff": diff})
            continue
        if not last:
            continue
        remaining = abs(diff)
        while remaining > 1:
            lot = min(remaining, delta_max)
            volume = int(lot // last // 100 * 100)
            if volume <= 0:
                break
            direction = "buy" if diff > 0 else "sell"
            if do_trade:
                res = await b.call_locked(b.gateway.place_order, code, direction, "limit",
                                           last, volume, "rebalance", f"rebal-{code}")
                orders.append({"code": code, "direction": direction, "volume": volume,
                               "price": last, "order": res})
            else:
                orders.append({"code": code, "direction": direction, "volume": volume, "price": last})
            remaining -= lot
    return ok({"orders": orders, "generated": len([o for o in orders if "direction" in o])})


# ---------------- 回测任务队列（真实 K 线） ----------------

