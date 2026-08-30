"""G7 条件选股 REST 端点。

- ``GET /market/screen``：条件选股（GET 只读、签名全简单类型 → auto-safe，
  经 G3 自动暴露为 MCP tool；Agent 可直接「帮我筛选 XX 条件的股票」）。
  ``conditions`` 为 URL 编码的 JSON 条件树（见 app.screener.conditions）。
- ``POST /market/screen/boards``：把选股结果存为动态板块（写操作，人工确认域）。
- ``GET /market/screen/boards``：列出已保存动态板块。

数据源为本地数据仓（G1-4）；仓为空返回 503 引导先同步（零 mock）。
"""
import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

from app.routes._common import audit_log, err, ok
from app.screener.engine import list_saved_boards, save_as_board, scan

router = APIRouter()


def _parse_conditions(raw: str) -> dict:
    try:
        cond = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"conditions 不是合法 JSON：{exc}") from exc
    if not isinstance(cond, dict):
        raise ValueError("conditions 顶层须为对象 {and/or/叶子}")
    return cond


@router.get("/market/screen")
async def market_screen(
    conditions: str,
    limit: int = 100,
    sort_by: str = "score",
    sort_desc: int = 1,
    adjust: str = "qfq",
    period: str = "1d",
    min_price: str = "",
    max_price: str = "",
    max_codes: int = 0,
):
    """条件选股：conditions 为 JSON 条件树（{and:[..]}/{or:[..]}/叶子），
    叶子={indicator:{name,params,output,op,value,window}} 或 {field:{name,op,value,window}}。
    本地数据仓全市场扫描（不走逐股远程），返回命中列表按 score/涨跌幅等排序。"""
    try:
        cond = _parse_conditions(conditions)
        min_p = float(min_price) if min_price else None
        max_p = float(max_price) if max_price else None
    except ValueError as exc:
        return err(400, str(exc))
    try:
        out = await asyncio.to_thread(
            scan, None, cond,
            limit=limit, sort_by=sort_by, sort_desc=bool(sort_desc),
            adjust=adjust, period=period, min_price=min_p, max_price=max_p,
            max_codes=max_codes)
    except (ValueError, RuntimeError) as exc:
        return err(400 if isinstance(exc, ValueError) else 503, str(exc))
    return ok(out)


@router.post("/market/screen/boards")
async def market_screen_boards_save(body: Dict[str, Any]):
    """把选股结果存为动态板块：{name, conditions, results:[{code,name,close,change_pct}]}。"""
    name = (body or {}).get("name")
    conditions = (body or {}).get("conditions")
    results = (body or {}).get("results")
    if not name or not isinstance(conditions, dict) or not isinstance(results, list):
        return err(400, "body 须含 name(str)/conditions(obj)/results(list)")
    try:
        saved = save_as_board(None, name, conditions, results)
    except ValueError as exc:
        return err(400, str(exc))
    audit_log("api", "market_screen_boards_save", name or "", body)

    return ok(saved)


@router.get("/market/screen/boards")
async def market_screen_boards_list():
    """列出已保存的动态板块（名称 + 成员数）。"""
    try:
        items = list_saved_boards()
    except RuntimeError as exc:
        return err(503, str(exc))          # 本地仓未初始化 → 业务 503，绝不裸抛
    return ok({"items": items, "count": len(items)})


@router.post("/market/screen/nl")
async def market_screen_nl(body: Dict[str, Any]):
    """G8 自然语言选股：{text} → 可编辑条件树（conditions/rules/unsupported）。
    条件树与 /market/screen 完全兼容，用户可回显修改后执行。"""
    from app.agent.nl_screen import parse_nl
    text = (body or {}).get("text")
    try:
        out = parse_nl(text)
    except ValueError as exc:
        return err(400, str(exc))
    return ok(out)
