"""G7 条件选股 REST 端点（Phase 4 增强）。

- ``GET /market/screen``：条件选股。新增 ``source_policy / universe / prefilter /
  fields / offline`` 参数；响应新增 ``provenance / degraded / degraded_reason /
  fallback_tried / provider_policy_version / dataset_snapshot_id / fundamentals``。
  数据源与本地仓皆不可用 → **503 + 原因**（零 mock，绝不返回空列表冒充「无符合标的」）。
  ★ ``classic``：改走**经典形态策略**（app/screener/classic.py），此时 ``conditions``
  可不传（传了也只回显、不参与求值）。
- ``GET /market/screen/strategies``：列举可用的经典策略（id/名称/说明/默认参数）。
- ``POST /market/screen/expr``：公式 DSL 选股（类终端公式 → conditions JSON，再走同一引擎）。
- ``POST /market/screen/classic``：经典策略选股（多策略批量，与定时任务同一内核）。
- ``POST /market/screen/boards`` / ``GET /market/screen/boards``：动态板块保存与列举。

所有取数经 ``app.data.bars_provider`` 与 ``app.screener`` 的能力链完成，路由层不出现 provider 名。
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.routes._common import audit_log, err, ok
from app.screener.classic import STRATEGY_IDS, list_strategies, run_classic
from app.screener.engine import list_saved_boards, save_as_board, scan_async
from app.screener.picks import bars_last_date, save_run

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


def _check_classic(strategy_id: str) -> None:
    """未知经典策略 → 400 + 可用清单（绝不静默返回空列表冒充「无命中」）。"""
    if strategy_id and strategy_id not in STRATEGY_IDS:
        raise ValueError(
            f"未知经典策略：{strategy_id}（可选 {', '.join(STRATEGY_IDS)}）")


def _parse_classic_params(raw: Any) -> Optional[dict]:
    """经典策略参数：JSON 字符串或对象均可；非法一律 400，绝不静默忽略。

    ★ 为什么不吞掉错误：参数写错却按默认值跑，用户会以为「策略没选出票」，
    实际是参数根本没生效 —— 这类静默失败比报错更难排查。
    """
    if raw in (None, "", {}):
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"classic_params 不是合法 JSON：{exc}") from exc
        if not isinstance(val, dict):
            raise ValueError("classic_params 顶层须为对象")
        return val
    raise ValueError("classic_params 须为对象或 JSON 字符串")


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
    conditions: str = "",
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
    classic: str = "",
    classic_params: str = "",
):
    """多源条件选股（或经典形态策略选股）。

    - ``conditions``：条件树 JSON；**走经典策略时可不传**（此时不参与求值）。
    - ``classic``：经典策略 id（见 ``GET /market/screen/strategies``）；非空时
      ``conditions`` 仅回显、不参与求值。
    - ``classic_params``：覆盖策略默认参数的 JSON（如 ``{"breakout_days": 30}``）。
    - ``source_policy``：auto / prefer_qmt / qmt_only / local_only / explicit:<id>；
      默认 auto（QMT → eltdx → baostock → akshare 按链降级）。
    - ``universe``：JSON 或 ``sector:医药`` / ``index:000300`` / ``custom`` + ``fields`` 等。
    - ``prefilter``：JSON，如 ``{"exclude_st": true}``（过滤前置，省取数）。
    - ``fields``：逗号分隔或 JSON 列表，附加基本面字段（带字段级溯源）。
    - ``offline``：1 时只走本地 canonical（仅供离线复现）。
    """
    try:
        _check_classic(classic)
        cparams = _parse_classic_params(classic_params)
        if classic:
            cond: dict = {}
        else:
            if not conditions:
                raise ValueError("conditions 必填（或改用 classic 指定经典策略）")
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
            prefilter=pre, fields=flds, offline=bool(offline),
            classic=classic, classic_params=cparams)
    except (ValueError, RuntimeError) as exc:
        return err(400 if isinstance(exc, ValueError) else 503, str(exc))
    return ok(out)


@router.get("/market/screen/strategies")
async def market_screen_strategies():
    """列举经典策略（id / 名称 / 说明 / 默认参数），供界面下拉与参数表单使用。"""
    return ok({"items": list_strategies(), "count": len(STRATEGY_IDS)})


@router.post("/market/screen/classic")
async def market_screen_classic(body: Dict[str, Any]):
    """经典策略选股（可一次跑多个策略），与定时任务 ``system.classic_screen``
    共用 ``screener.classic.run_classic`` 内核 —— 手动跑与定时跑结果口径一致。

    请求：{strategies:["turtle_trade",...], params?:{策略id:{k:v}}, limit?,
    universe?, source_policy?, adjust?, period?, max_codes?, offline?}。
    响应按策略 id 分组：{results:{sid:[row...]}, scanned, total_hits}。
    """
    b = body or {}
    raw = b.get("strategies") or b.get("strategy") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return err(400, "body.strategies 必须为非空数组（或 body.strategy 字符串）")
    strategies = [str(x) for x in raw if x]
    try:
        for sid in strategies:
            _check_classic(sid)
        per = b.get("params") or {}
        if not isinstance(per, dict):
            raise ValueError("body.params 须为 {策略id: {参数}} 对象")
        cparams = _parse_classic_params(per)
    except ValueError as exc:
        return err(400, str(exc))

    limit = int(b.get("limit") or 50)
    period = str(b.get("period") or "1d")
    adjust = str(b.get("adjust") or "qfq")
    policy = str(b.get("source_policy") or "auto")
    try:
        from app.data.bars_provider import BarsProvider
        from app.screener.universe import UniverseSpec, resolve_universe

        uni = await resolve_universe(
            UniverseSpec.parse(b.get("universe")), policy_str=policy)
        codes = uni["codes"]
        if not codes:
            raise RuntimeError(
                "选股股票池为空（数据源不可用且本地无股票列表）——请先同步日线数据")
        max_codes = int(b.get("max_codes") or 0)
        if max_codes and max_codes > 0:
            codes = codes[:max_codes]
        bp = BarsProvider()
        bars_map, report = await bp.get_bars_batch(
            codes, period=period, adjust=adjust, policy_str=policy,
            offline=bool(b.get("offline")), lite=True)
        if not bars_map:
            raise RuntimeError("no_data_source_and_no_local_data：请连接数据源或先运行同步任务")

        results: Dict[str, List[Any]] = {}
        for sid in strategies:
            # 单策略参数：params[strategy] 优先于 params 的平铺键
            one = dict(cparams or {})
            inner = (per.get(sid) if isinstance(per, dict) else None)
            if isinstance(inner, dict):
                one.update(inner)
            results[sid] = await asyncio.to_thread(
                run_classic, bars_map, sid, one or None, limit,
                uni.get("names") or {})
    except (ValueError, RuntimeError) as exc:
        return err(400 if isinstance(exc, ValueError) else 503, str(exc))
    # ★ 落库：手动选股的结果也必须留下（关掉页面就没了 ⇒ 第二天想看「昨天选出来什么」
    #   只能重跑，而重跑用的是**今天**的行情，结果自然不同）。
    saved = save_run(
        results=results, scanned=len(bars_map), source="manual",
        bar_date=bars_last_date(bars_map),
        degraded=bool(report.degraded),
        degraded_reason=report.degraded_reason or "",
        # 名称兜底：即使某策略行没带 name，也按股票池的名称表补上
        names=uni.get("names") or {},
    )
    return ok({
        "strategies": strategies,
        "scanned": len(bars_map),
        "total_hits": sum(len(v) for v in results.values()),
        "results": results,
        "degraded": report.degraded,
        "degraded_reason": report.degraded_reason or "",
        "provenance": report.to_provenance(),
        "run": saved,
    })


@router.get("/market/screen/classic/picks")
async def market_screen_classic_picks(run_id: str = "", strategy: str = "",
                                      source: str = "", limit: int = 500):
    """最近一次（或指定 ``run_id``）的选股结果 —— 「自动选股」页签的数据源。

    ★ 为什么需要它：定时选股 ``system.classic_screen`` 跑完之后，用户此前只能去
    「任务运行时」翻一个巨大的 JSON 结果字段，翻不到就等于没有。本端点让结果
    **有稳定的界面**：按运行批次查看命中、命中理由、以及**可信度元数据**
    （``scanned`` / ``bar_date`` / ``degraded_*``）。

    ``run_id`` 为空 → 取最近一次运行；库里没有任何记录 → ``run: null``
    （前端显示「尚未跑过选股」，**不是**空表格 —— 空表格会被读成「今天没选出票」）。
    """
    from app.screener.picks import picks_of_run

    try:
        out = picks_of_run(run_id, strategy=strategy,
                           limit=int(limit or 500), source=source)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"读取选股结果失败：{exc}")
    return ok(out)


@router.post("/market/screen/expr")
async def market_screen_expr(body: Dict[str, Any]):
    """公式 DSL 选股（类终端公式 → 条件树 → 同引擎）。

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
