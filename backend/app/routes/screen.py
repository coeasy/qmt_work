"""G7 条件选股 REST 端点（Phase 4 增强）。

- ``GET /market/screen``：条件选股。新增 ``source_policy / universe / prefilter /
  fields / offline`` 参数；响应新增 ``provenance / degraded / degraded_reason /
  fallback_tried / provider_policy_version / dataset_snapshot_id / fundamentals``。
  数据源与本地仓皆不可用 → **503 + 原因**（零 mock，绝不返回空列表冒充「无符合标的」）。
- ``POST /market/screen/expr``：公式 DSL 选股（类通达信公式 → conditions JSON，再走同一引擎）。
- ``POST /market/screen/boards`` / ``GET /market/screen/boards``：动态板块保存与列举。

所有取数经 ``app.data.bars_provider`` 与 ``app.screener`` 的能力链完成，路由层不出现 provider 名。
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.routes._common import audit_log, err, ok
from app.screener.engine import list_saved_boards, save_as_board, scan_async

router = APIRouter()


def _parse_json(raw: str, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _parse_conditions(raw: str) -> dict:
    try:
        cond = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"conditions 不是合法 JSON：{exc}") from exc
    if not isinstance(cond, dict):
        raise ValueError("conditions 顶层须为对象 {and/or/叶子}")
    return cond


def _parse_fields(raw: str) -> Optional[List[str]]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except (TypeError, ValueError):
        pass
    return [s.strip() for s in raw.split(",") if s.strip()]


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
    source_policy: str = "auto",
    universe: str = "",
    prefilter: str = "",
    fields: str = "",
    offline: int = 0,
):
    """多源条件选股。

    - ``source_policy``：auto / prefer_qmt / qmt_only / local_only / explicit:<id>；
      默认 auto（QMT → eltdx → baostock → akshare 按链降级）。
    - ``universe``：JSON 或 ``sector:医药`` / ``index:000300`` / ``custom`` + ``fields`` 等。
    - ``prefilter``：JSON，如 ``{"exclude_st": true}``（过滤前置，省取数）。
    - ``fields``：逗号分隔或 JSON 列表，附加基本面字段（带字段级溯源）。
    - ``offline``：1 时只走本地 canonical（仅供离线复现）。
    """
    try:
        cond = _parse_conditions(conditions)
        min_p = float(min_price) if min_price else None
        max_p = float(max_price) if max_price else None
        uni = _parse_json(universe, None)
        pre = _parse_json(prefilter, None)
        flds = _parse_fields(fields)
    except ValueError as exc:
        return err(400, str(exc))
    try:
        out = await scan_async(
            None, cond,
            limit=limit, sort_by=sort_by, sort_desc=bool(sort_desc),
            adjust=adjust, period=period, min_price=min_p, max_price=max_p,
            max_codes=max_codes, source_policy=source_policy, universe=uni,
            prefilter=pre, fields=flds, offline=bool(offline))
    except (ValueError, RuntimeError) as exc:
        return err(400 if isinstance(exc, ValueError) else 503, str(exc))
    return ok(out)


@router.post("/market/screen/expr")
async def market_screen_expr(body: Dict[str, Any]):
    """公式 DSL 选股（类通达信公式 → 条件树 → 同引擎）。

    请求：{expr, limit?, sort_by?, adjust?, source_policy?, universe?, ...}。
    公式示例：``"C > MA(20) AND RSI(14) < 30"``。
    截面算子 ``RANK(x)`` / ``TOP(x,n)`` 由 conditions 评估层在后续版本接入；
    当前本端点与 /market/screen 共用同一条件求值内核。
    """
    expr = (body or {}).get("expr")
    if not expr or not isinstance(expr, str):
        return err(400, "body.expr 必须为非空字符串")
    try:
        from app.indicators.dsl import parse as parse_formula
        cond = parse_formula(expr)
    except ValueError as exc:
        return err(400, f"公式解析失败：{exc}")
    try:
        out = await scan_async(
            None, cond,
            limit=int(body.get("limit", 100)),
            sort_by=body.get("sort_by", "score"),
            sort_desc=bool(body.get("sort_desc", 1)),
            adjust=body.get("adjust", "qfq"),
            period=body.get("period", "1d"),
            min_price=(float(body["min_price"]) if body.get("min_price") else None),
            max_price=(float(body["max_price"]) if body.get("max_price") else None),
            max_codes=int(body.get("max_codes", 0) or 0),
            source_policy=body.get("source_policy", "auto"),
            universe=body.get("universe"),
            prefilter=body.get("prefilter"),
            fields=body.get("fields"),
            offline=bool(body.get("offline", 0)),
        )
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


__all__ = ["router"]
