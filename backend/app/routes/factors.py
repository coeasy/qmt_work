"""因子 / 指标库路由（P1）。

- GET  /factors                列出所有可用指标
- POST /factors/compute        计算单个指标
- POST /factors/compute/many   批量计算多个指标
- POST /factors/from-kline     基于**真实**券商 K 线计算指标（不伪造数据）

标签前缀统一为 /factors；由集成方挂载到 /api/v1。
"""
from app.routes._common import ok, err, state
from fastapi import APIRouter
from typing import Any, Dict, List, Optional

from tools import fetch_kline_cached
from tools.factors import compute_factor, compute_many, list_factors, from_kline
from xtquant_client.base import BrokerError

router = APIRouter()


@router.get("/factors")
async def get_factors():
    """列出全部可用指标及其默认参数。"""
    return ok(list_factors())


@router.post("/factors/compute")
async def post_compute(body: Dict[str, Any]):
    """计算单个指标。

    请求体: {"name": str, "values": [..], "params": {..} 可选}
    """
    name = body.get("name")
    values = body.get("values")
    params = body.get("params") or {}
    if not name:
        return err(400, "缺少 name")
    if values is None:
        return err(400, "缺少 values")
    try:
        result = compute_factor(name, values, **params)
    except ValueError as exc:
        return err(400, str(exc))
    return ok({"name": name, "values": result})


@router.post("/factors/compute/many")
async def post_compute_many(body: Dict[str, Any]):
    """批量计算多个指标。

    请求体: {"names": [...], "values": [...], "params": {..} 可选}
    params 可携带 high/low/volume 等共享序列。
    """
    names = body.get("names") or []
    values = body.get("values")
    params = body.get("params") or {}
    if not names:
        return err(400, "缺少 names")
    if values is None:
        return err(400, "缺少 values")
    try:
        results = compute_many(names, values, **params)
    except ValueError as exc:
        return err(400, str(exc))
    return ok(results)


@router.post("/factors/from-kline")
async def post_from_kline(body: Dict[str, Any]):
    """基于真实券商 K 线计算指标（不伪造数据）。

    请求体: {"symbol": str, "period"?: str, "count"?: int, "broker_id"?: str,
             "names": [...], "params"?: {..}}
    """
    symbol = body.get("symbol")
    if not symbol:
        return err(400, "缺少 symbol")
    period = body.get("period") or "1d"
    count = int(body.get("count") or 250)
    broker_id = body.get("broker_id") or None
    names = body.get("names") or []
    params = body.get("params") or {}
    if not names:
        return err(400, "缺少 names")

    try:
        res = await fetch_kline_cached(symbol, period, count, broker_id=broker_id)
    except BrokerError as exc:
        return err(503, str(exc))

    bars = res.get("bars") or []
    if not bars:
        return err(503, "未连接券商或该标的无历史K线")

    # 抽取真实 K 线字段序列
    close = from_kline(bars, "close")
    shared: Dict[str, Any] = {"close": close}
    for fld in ("high", "low", "volume"):
        shared[fld] = from_kline(bars, fld)

    out: Dict[str, Any] = {}
    for name in names:
        fp = dict(params)
        # 把对应额外序列注入 params
        for fld in ("high", "low", "volume"):
            if fld in shared:
                fp.setdefault(fld, shared[fld])
        try:
            out[name] = compute_factor(name, close, **fp)
        except ValueError as exc:
            return err(400, str(exc))
    return ok({"symbol": symbol, "period": period, "count": len(bars),
               "source": res.get("source"), "values": out})
