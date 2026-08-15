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

@router.post("/trade/order")
async def trade_order(body: dict):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    code = str(body.get("code", "")).strip().upper()
    direction = (body.get("direction") or "buy").lower()
    volume = int(body.get("volume", 0))
    price = float(body.get("price", 0) or 0)
    price_type = body.get("price_type", "limit")
    if not code:
        return err(400, "code 必填")
    if direction not in ("buy", "sell"):
        return err(400, "direction 须为 buy/sell")
    params = {"code": code, "direction": direction, "volume": volume,
              "price": price, "price_type": price_type,
              "strategy": body.get("strategy_name", "manual"),
              "remark": body.get("remark", ""), "broker_id": ""}
    okc, reason = state.risk.check_order(code, price if price > 0 else 100.0, volume, direction)
    if not okc:
        state.db.audit("trading", "order.rejected", code, params, reason)
        return err(400, f"风控拒绝：{reason}")
    res = await _call(b, b.gateway.place_order, code, direction, price_type,
                      price, volume, "manual", body.get("remark", ""))
    if isinstance(res, dict) and res.get("code", 0) != 0:
        return res
    state.db.audit("trading", "order.submitted", code, params,
                   f"order_id={res.get('order_id')}")
    return ok(res)

@router.post("/trade/cancel")
async def trade_cancel(body: dict):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    oid = str(body.get("order_id", ""))
    if not oid:
        return err(400, "order_id 必填")
    res = await _call(b, b.gateway.cancel_order, oid)
    state.db.audit("trading", "order.cancel", oid, {}, "ok")
    return ok(res)

@router.get("/trade/positions")
async def trade_positions(symbol: str = ""):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_positions, symbol or None)

@router.get("/trade/orders")
async def trade_orders():
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_orders)

@router.get("/trade/deals")
async def trade_deals():
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_deals)

@router.post("/trade/target")
async def trade_target(body: dict):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    from tools.position import order_target_position
    try:
        return ok(await order_target_position(
            str(body.get("code", "")), float(body.get("target_pct", 0)),
            float(body.get("price", 0) or 0), bool(body.get("do_trade", False))))
    except Exception as exc:  # noqa: BLE001
        return err(400, str(exc))


# ---------------- 条件单（Trade 页） ----------------

@router.get("/trade/conditions")
async def trade_conditions():
    e = state.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    return ok(e.status())

@router.post("/trade/conditions")
async def trade_condition_submit(body: dict):
    e = state.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    try:
        return ok(e.submit(body.get("code", ""), body.get("side", "buy"),
                           body.get("trigger_type", "gte"),
                           float(body.get("trigger_price", 0)),
                           int(body.get("volume", 0)),
                           body.get("price_type", "market"),
                           float(body.get("price", 0) or 0),
                           body.get("remark", ""),
                           valid_days=int(body.get("valid_days", 0) or 0)))
    except ValueError as exc:
        return err(400, str(exc))

@router.post("/trade/conditions/{cid}/cancel")
async def trade_condition_cancel(cid: str):
    e = state.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    try:
        return ok(e.cancel(cid))
    except KeyError as exc:
        return err(404, str(exc))


# ---------------- 同步测试辅助 ----------------

