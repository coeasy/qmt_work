from app.routes._common import ok, err, state, _need, _call, BrokerError

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

@router.get("/market/quote")
async def market_quote(code: str, conn_id: str = ""):
    """实时行情快照（最新价 / 涨跌幅 / 成交量 / 买卖五档）。"""
    b = _need(conn_id or None)
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_quote, code)

@router.get("/market/kline")
async def market_kline(code: str, period: str = "1d", count: int = 250,
                       conn_id: str = "", force: bool = False):
    """历史 K 线（C1 本地缓存优先；source 标注 cache / broker / cache_stale）。"""
    from tools import fetch_kline_cached
    try:
        res = await fetch_kline_cached(code, period, count,
                                       broker_id=conn_id or None, force=force)
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"code": code, "period": period, "count": len(res.get("bars") or []),
               "source": res.get("source"), "cached_at": res.get("cached_at"),
               "note": res.get("note"), "bars": res.get("bars") or []})

@router.get("/market/limitup")
async def market_limitup(sector: str = "沪深A股", min_pct: float = 9.5,
                         only_limit: bool = True, limit: int = 200,
                         sort: str = "change"):
    """涨停板：扫描板块内最新行情，列出涨停（或接近涨停）个股及最新数据，便于快速选股交易。"""
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    try:
        from tools.limitup import scan_limit_up
        rows = await scan_limit_up(b, sector, min_pct, only_limit, limit, sort)
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"sector": sector, "count": len(rows), "rows": rows})

@router.get("/market/kline/cache")
async def kline_cache_stats():
    """K 线缓存统计（行数、序列数、命中率）。"""
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    return ok(state.kline_cache.stats())

@router.delete("/market/kline/cache")
async def kline_cache_clear(code: str = "", period: str = ""):
    """清理 K 线缓存（可按 code / code+period 精确清理）。"""
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    n = state.kline_cache.clear(code=code, period=period)
    state.db.audit("admin", "kline_cache.clear", code or "*",
                   {"period": period}, f"deleted={n}")
    return ok({"deleted": n})


# ---------------- 行情爬虫（真实 K 线落库） ----------------

@router.post("/market/crawl")
async def crawl_market(body: dict):
    b = _need(body.get("conn_id") or None)
    if b is None:
        return err(503, "未连接任何券商客户端。")
    codes = body.get("codes", ["600519.SH"])
    days = int(body.get("days", 30))
    inserted = 0
    for code in codes:
        bars = await _call(b, b.gateway.get_kline, code, "1d", days)
        if isinstance(bars, dict) and bars.get("code"):
            return bars
        for bb in bars:
            try:
                state.db.upsert("market_cache", {
                    "code": code, "dtype": "kline", "ts": bb.get("time", ""),
                    "payload_json": json.dumps(bb, ensure_ascii=False)})
                inserted += 1
            except Exception:
                pass
    return ok({"crawled_codes": codes, "bars_inserted": inserted})


# ---------------- LLM 配置（加密存储） ----------------

@router.get("/market/l2")
async def market_l2(code: str, count: int = 100):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_l2_transactions, code, count)


# ---------------- 策略模板库 ----------------

