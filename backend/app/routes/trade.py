from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import (MSG_NO_BROKER, _call, _need, envelope_ok, err,
                                no_broker, ok)

# --- stdlib imports injected by fix_route_imports ---
from datasource.registry import get_manager
from gateway.execution import get_execution_service
from gateway.idempotency import single_flight


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
async def trade_order(body: dict, ctx: AppContext = Depends(get_ctx)):
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
    if ctx.signal_router is None:
        return err(503, "统一信号入口未初始化")
    # V9 Execution Unification：手动单同样经 SignalRouter 统一链路
    # （ExecutionMode live/paper/dry_run + 风控 + 幂等 + WAL + 审计），
    # 不再直连 ExecutionService 绕过信号模式。auto_confirm=True 保持原有
    # 手动单 UX（无 TOTP 挂起），大额确认语义与引擎单一致由配置决定。
    async def _run():
        # conn_id 透传：单笔下单必须支持指定账户（broker_id 即连接选择器），
        # 不传则回落到 active 连接（由 signal_router 处理）。修复 P0-14：原先
        # 硬编码 broker_id="" 导致多账户环境下单恒走 active、无法指定账户。
        conn_id = str(body.get("conn_id", "") or "")
        res = await ctx.signal_router.submit(
            code, direction, volume, price, price_type, source="manual",
            broker_id=conn_id, remark=str(body.get("remark", "") or ""),
            idempotency_key=str(idem or ""), auto_confirm=True)
        if isinstance(res, dict) and conn_id:
            res["conn_id"] = conn_id
        if isinstance(res, dict) and not res.get("ok", True):
            reason = str(res.get("reason") or res.get("message") or res)
            # 语义分流（零 mock 契约）：
            # - 券商**不可用**（未连接 / SDK 缺失 / 桥接不可用）→ 503 + 「券商连接」引导；
            # - 其余为**真实执行拒绝**（风控拦截 / 柜台拒单 / 非交易时段）→ 400 + 真实原因。
            # 铁律：绝不把失败粉饰成 code=0（前端 tradeApi.js 依赖 code!=0 抛错，
            # 否则会把拒单显示成「已报」——假成功）。
            if res.get("broker_unavailable"):
                return err(503, f"{MSG_NO_BROKER}（{reason}）")
            return err(400, f"风控/执行拒绝：{reason}")
        return ok(res)

    # 幂等：显式 idempotency_key（前端/重试传）优先；否则按委托内容哈希去重，
    # 双击/超时重试同参数在窗口内只下一单，杜绝重复成交。
    key = idem or f"manual:{code}:{direction}:{volume}:{price}:{price_type}"
    return await single_flight(key, _run, window=5.0)

@router.post("/trade/cancel")
async def trade_cancel(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交trade / cancel（POST /trade/cancel）。"""
    b = _need()
    if b is None:
        return no_broker()
    oid = str(body.get("order_id", ""))
    if not oid:
        return err(400, "order_id 必填")
    res = await get_execution_service().cancel_order(b, oid)
    return ok(res)

@router.get("/trade/positions")
async def trade_positions(symbol: str = "", ctx: AppContext = Depends(get_ctx)):
    """获取trade / positions（GET /trade/positions）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_positions, symbol or None)
    return envelope_ok(_enrich_names(res)) if isinstance(res, list) else res

@router.get("/trade/orders")
async def trade_orders(ctx: AppContext = Depends(get_ctx)):
    """获取trade / orders（GET /trade/orders）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_orders)
    return envelope_ok(_enrich_names(res)) if isinstance(res, list) else res

@router.get("/trade/deals")
async def trade_deals(ctx: AppContext = Depends(get_ctx)):
    """获取trade / deals（GET /trade/deals）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_deals)
    return envelope_ok(_enrich_names(res)) if isinstance(res, list) else res

@router.post("/trade/target")
async def trade_target(body: dict, ctx: AppContext = Depends(get_ctx)):
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
async def trade_precheck(body: dict, ctx: AppContext = Depends(get_ctx)):
    """风控预检（非变更型）：判断一笔委托是否会被风控放行，但不计入日级计数/频率窗口。"""
    if ctx.risk is None:
        return err(503, "风控未初始化")
    code = str(body.get("code", "")).strip().upper()
    direction = (body.get("direction") or "buy").lower()
    try:
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
    except (TypeError, ValueError):
        return err(400, "volume/price 必须为数字")
    price_type = body.get("price_type", "limit")
    allowed, reason = ctx.risk.precheck_order(code, price, volume, direction, price_type)
    return ok({"allowed": allowed, "reason": reason})


@router.get("/trade/conditions")
async def trade_conditions(ctx: AppContext = Depends(get_ctx)):
    """获取trade / conditions（GET /trade/conditions）。"""
    e = ctx.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    return ok(e.status())

@router.post("/trade/conditions")
async def trade_condition_submit(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交trade / conditions（POST /trade/conditions）。"""
    e = ctx.condition_engine
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
async def trade_condition_cancel(cid: str, ctx: AppContext = Depends(get_ctx)):
    """创建/提交trade / conditions / cancel（POST /trade/conditions/{cid}/cancel）。"""
    e = ctx.condition_engine
    if e is None:
        return err(503, "条件单引擎未初始化")
    try:
        return ok(e.cancel(cid))
    except KeyError as exc:
        return err(404, str(exc))


# ---------------- 同步测试辅助 ----------------

