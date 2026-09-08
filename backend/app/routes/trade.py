from fastapi import APIRouter

from app.routes._common import _call, _need, err, no_broker, ok, state, envelope_ok
from gateway.idempotency import single_flight

# --- stdlib imports injected by fix_route_imports ---

from datasource.registry import get_manager


def _enrich_names(rows):
    """用 eltdx 名称表 O(1) 兜底富化券商返回的裸代码 name（持仓/委托/成交常只剩代码）。

    仅当 name 缺失或回退成代码本身时查表补全；查不到保持原值，绝不伪造。
    """
    if not isinstance(rows, list):
        return rows
    try:
        mgr = get_manager()
    except Exception:  # noqa: BLE001
        return rows
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = r.get("code") or r.get("stock_code") or r.get("symbol") or ""
        if not code:
            continue
        nm = r.get("name")
        bare = str(code).split(".")[0].upper()
        if not nm or str(nm).upper() in (bare, str(code).upper()):
            looked = mgr.lookup_name(code)
            if looked:
                r["name"] = looked
    return rows


router = APIRouter()

@router.post("/trade/order")
async def trade_order(body: dict):
    """创建/提交trade / order（POST /trade/order）。"""
    b = _need()
    if b is None:
        return no_broker()
    code = str(body.get("code", "")).strip().upper()
    direction = (body.get("direction") or "buy").lower()
    volume = int(body.get("volume", 0))
    price = float(body.get("price", 0) or 0)
    price_type = body.get("price_type", "limit")
    idem = body.get("idempotency_key") or body.get("client_order_id") or ""
    if not code:
        return err(400, "code 必填")
    if direction not in ("buy", "sell"):
        return err(400, "direction 须为 buy/sell")
    params = {"code": code, "direction": direction, "volume": volume,
              "price": price, "price_type": price_type,
              "strategy": body.get("strategy_name", "manual"),
              "remark": body.get("remark", ""), "broker_id": ""}

    async def _run():
        okc, reason = state.risk.check_order(code, price, volume, direction, price_type)
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

    # 幂等：显式 idempotency_key（前端/重试传）优先；否则按委托内容哈希去重，
    # 双击/超时重试同参数在 5s 窗口内只下一单，杜绝重复成交。
    key = idem or f"order:{code}:{direction}:{volume}:{price}:{price_type}"
    return await single_flight(key, _run, window=5.0)

@router.post("/trade/cancel")
async def trade_cancel(body: dict):
    """创建/提交trade / cancel（POST /trade/cancel）。"""
    b = _need()
    if b is None:
        return no_broker()
    oid = str(body.get("order_id", ""))
    if not oid:
        return err(400, "order_id 必填")
    res = await _call(b, b.gateway.cancel_order, oid)
    state.db.audit("trading", "order.cancel", oid, {}, "ok")
    return ok(res)

@router.get("/trade/positions")
async def trade_positions(symbol: str = ""):
    """获取trade / positions（GET /trade/positions）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_positions, symbol or None)
    return envelope_ok(_enrich_names(res)) if isinstance(res, list) else res

@router.get("/trade/orders")
async def trade_orders():
    """获取trade / orders（GET /trade/orders）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_orders)
    return envelope_ok(_enrich_names(res)) if isinstance(res, list) else res

@router.get("/trade/deals")
async def trade_deals():
    """获取trade / deals（GET /trade/deals）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_deals)
    return envelope_ok(_enrich_names(res)) if isinstance(res, list) else res

@router.post("/trade/target")
async def trade_target(body: dict):
    """创建/提交trade / target（POST /trade/target）。"""
    b = _need()
    if b is None:
        return no_broker()
    from tools.position import order_target_position
    try:
        return ok(await order_target_position(
            str(body.get("code", "")), float(body.get("target_pct", 0)),
            float(body.get("price", 0) or 0), bool(body.get("do_trade", False))))
    except Exception as exc:  # noqa: BLE001
        return err(400, str(exc))


# ---------------- 条件单（Trade 页） ----------------

@router.post("/trade/precheck")
async def trade_precheck(body: dict):
    """风控预检（非变更型）：判断一笔委托是否会被风控放行，但不计入日级计数/频率窗口。"""
    if state.risk is None:
        return err(503, "风控未初始化")
    code = str(body.get("code", "")).strip().upper()
    direction = (body.get("direction") or "buy").lower()
    try:
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
    except (TypeError, ValueError):
        return err(400, "volume/price 必须为数字")
    price_type = body.get("price_type", "limit")
    allowed, reason = state.risk.precheck_order(code, price, volume, direction, price_type)
    return ok({"allowed": allowed, "reason": reason})


@router.get("/trade/conditions")
async def trade_conditions():
    """获取trade / conditions（GET /trade/conditions）。"""
    e = state.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    return ok(e.status())

@router.post("/trade/conditions")
async def trade_condition_submit(body: dict):
    """创建/提交trade / conditions（POST /trade/conditions）。"""
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
    """创建/提交trade / conditions / cancel（POST /trade/conditions/{cid}/cancel）。"""
    e = state.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    try:
        return ok(e.cancel(cid))
    except KeyError as exc:
        return err(404, str(exc))


# ---------------- 同步测试辅助 ----------------

