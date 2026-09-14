import logging

from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import (MSG_NO_BROKER, _call, _need, envelope_ok, err,
                                no_broker, ok)
# 持仓行富化（中文名兜底 + 现价/盈亏/盈亏比补算）已抽到服务层：
# /trade/positions 与 /account/status（仪表盘「持仓盈亏」）必须共用同一实现，
# 否则两页会各说各话（实测：持仓页 -10.4、仪表盘 0）。
from app.services.positions import enrich_names, enrich_positions

# --- stdlib imports injected by fix_route_imports ---
from gateway.execution import get_execution_service
from gateway.idempotency import single_flight

log = logging.getLogger("qmt_work.routes.trade")

# 兼容别名：既有调用方/测试仍以 routes.trade._enrich_names 引用
_enrich_names = enrich_names

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
    # 不再直连 ExecutionService 绕过信号模式。
    #
    # ★ P0-4 修复（2026-09-13）：此前此处硬编码 auto_confirm=True，语义是
    #   「跳过人工 TOTP 挂起」。但 auto_confirm 的设计意图是给**已授权的自动化
    #   引擎**用（algo/condition/limitup/strategy/rebalance —— 下单前已由用户
    #   配置并授权，无需逐单确认）。手动单同样传 True 的后果是：
    #   signal_router.route() 里 `amount >= threshold and not auto_confirm`
    #   这一条件**永远不成立**，于是**人工大额单的二次确认从不触发**，
    #   TOTP 形同虚设，前端 Trade.tsx 里整套 pending_confirmation / TOTP 交互
    #   成了死代码。
    #
    #   现改为 auto_confirm=False：手动单走完整语义 —— 金额 ≥
    #   signal_confirm_threshold（默认 10 万）且 mode ∈ {live, paper} 时，
    #   返回 pending_confirmation + confirm_token，由前端弹 TOTP 确认框，
    #   再调 /signal/confirm 执行。小额单不受影响，照常直接下单。
    #   引擎单（algo/limitup/... 各自传 True）行为完全不变。
    async def _run():
        # conn_id 透传：单笔下单必须支持指定账户（broker_id 即连接选择器），
        # 不传则回落到 active 连接（由 signal_router 处理）。修复 P0-14：原先
        # 硬编码 broker_id="" 导致多账户环境下单恒走 active、无法指定账户。
        conn_id = str(body.get("conn_id", "") or "")
        res = await ctx.signal_router.submit(
            code, direction, volume, price, price_type, source="manual",
            broker_id=conn_id, remark=str(body.get("remark", "") or ""),
            idempotency_key=str(idem or ""), auto_confirm=False)
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
    """获取trade / positions（GET /trade/positions）。

    券商只回 code/name/volume/avail/cost/market_value；本接口在此之上按真实行情
    补算 price / profit / profit_pct（拿不到行情则留空，见 app.services.positions）。
    """
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_positions, symbol or None)
    return (envelope_ok(await enrich_positions(res, ctx, b))
            if isinstance(res, list) else res)

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

