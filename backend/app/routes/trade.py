import logging

from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import (MSG_NO_BROKER, _call, _need, envelope_ok, err,
                                no_broker, ok)
# 持仓行富化（中文名兜底 + 现价/盈亏/盈亏比补算）已抽到服务层：
# /trade/positions 与 /account/status（仪表盘「持仓盈亏」）必须共用同一实现，
# 否则两页会各说各话（实测：持仓页 -10.4、仪表盘 0）。
from app.services.positions import enrich_names, enrich_positions
from app.services.order_contract import normalize_deals, normalize_orders

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
    if ctx.signal_router is None:
        return err(503, "统一信号入口未初始化")
    # ★★ 券商前置门必须按**执行模式**分流（2026-09-20 实测修复）。
    #
    # 此前此处是**无条件**的 `b = _need(); if b is None: return no_broker()`，
    # 于是「信号模式 = 模拟盘 / 预演」时，交易页下单**仍被「未连接券商」挡死**。
    # 但这两个模式**根本不需要券商**：
    #   - paper   ：成交由 PaperEngine 本地撮合（现金/持仓/T+1/涨跌停全在本地维护）；
    #   - dry_run ：只返回计划，`_route_inner` 在碰券商之前就 return 了。
    # 实测（本机真实库、`/signal/mode` = paper、未连券商）：
    #   POST /trade/order → 503「未连接任何券商客户端：请到「券商连接」页添加并连接券商。」
    # 即 **模拟盘这条链路从界面完全不可达** —— 用户被告知去连券商，而其实模拟盘
    # 本来就能成交。这与 `/trade/precheck`（已按 `require_account=_live` 分流）自相
    # 矛盾：预检说「可以下单」，下单却说「没连券商」。
    #
    # 现在只有 live 模式才要求券商。live 无券商时不再由这里拦，而是交给
    # `SignalRouter._live()` 返回 `broker_unavailable=True`，下面照旧映射成
    # 503 + 「券商连接」引导 —— **归因不变**，只是不再误伤 paper/dry_run。
    _mode = str(getattr(ctx.signal_router, "mode", "") or "")
    if _mode == "live" and _need() is None:
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
    """创建/提交trade / cancel（POST /trade/cancel）。

    ★ ``conn_id`` 必须生效：此前只读 ``order_id``、恒用**当前活跃连接**，
    前端传上来的 ``conn_id`` 被直接忽略 —— 多账户下这在两个方向上都危险：
    委托号在别的账户里撞号时撤错单，或根本撤不到却报成功。

    ★ 指定了 ``conn_id`` 却取不到该连接时**报错，绝不静默回退到 active** ——
    静默回退正是「撤错账户」的成因。

    ★ 参数校验在券商门**之前**，且券商门按**执行模式**分流（2026-09-20 实测修复）。
    此前顺序相反，导致两个错误归因：``{}`` 被券商门拦成「未连接任何券商客户端」
    （把「参数没传」说成「环境不可用」）；``paper`` 模式下撤模拟盘委托也被告知
    「请去连接券商」——而模拟盘委托即时撮合、本来就不存在可撤的挂单。
    """
    oid = str(body.get("order_id", ""))
    if not oid:
        return err(400, "order_id 必填")
    conn_id = str(body.get("conn_id") or "")
    _mode = str(getattr(ctx.signal_router, "mode", "") or "")
    # ★ 模拟盘委托号递给券商柜台 ⇒ 必须在此拦下并**如实归因**（2026-09-20 实测修复）。
    #
    #   实测（本机、QMT 已连接、signal mode=paper）：把模拟盘委托号 ``PAPER-1``
    #   传给 /trade/cancel，请求会一路走到券商适配器 ``int(order_id)`` ⇒
    #   ``ValueError: invalid literal for int()`` ⇒ 全局兜底 500「服务器内部错误」。
    #   用户看到的是「服务坏了」，真相是「模拟盘的委托根本不在券商那儿」。
    #
    #   ⚠️ 只拦**模拟盘前缀**的委托号，不按模式一刀切：用户可能刚从 live 切到 paper，
    #   此时券商那边还有真实挂单需要撤 —— 硬拦会让真实委托被搁死。
    from engines.paper_engine import PAPER_ORDER_PREFIX

    if oid.upper().startswith(PAPER_ORDER_PREFIX):
        return err(503, "该委托号属于「模拟盘」（本地撮合），券商柜台不存在这笔委托，"
                        "无可撤销的挂单。撤单只对券商实盘委托有效。"
                        + ("" if _mode == "live" else f"（当前信号模式：{_mode}）"))
    b = _need(conn_id or None)
    if b is None:
        if conn_id:
            return err(404, f"指定的连接不可用或未连接：{conn_id}（已阻止回退到其他账户）")
        if _mode and _mode != "live":
            return err(503, "当前信号模式为「模拟盘 / 预演」：模拟盘委托即时撮合、"
                            "预演只生成计划，不存在可撤销的挂单。"
                            "撤单只对券商实盘委托有效。")
        return no_broker()
    try:
        res = await get_execution_service().cancel_order(b, oid)
    except ValueError as exc:
        # ★ 委托号形态被柜台拒绝（非数字、长度不对…）是**参数问题**，不是服务故障。
        #   此前直接冒泡 ⇒ 500「服务器内部错误」，把「你传错了」说成「后端挂了」。
        return err(400, f"撤单被拒绝（委托号不被柜台接受）：{exc}")
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
    """获取trade / orders（GET /trade/orders）。

    ★ 字段契约归一（app.services.order_contract）：券商原生名是
    ``direction`` / ``dealt``，界面与消费方读 ``side`` / ``filled``；两者**都给**，
    既有调用方不受影响。``status`` 一律归一到平台标准词表
    （partial/cancelled/...），原始值留 ``status_raw``。
    """
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_orders)
    return envelope_ok(normalize_orders(_enrich_names(res))) if isinstance(res, list) else res

@router.get("/trade/deals")
async def trade_deals(ctx: AppContext = Depends(get_ctx)):
    """获取trade / deals（GET /trade/deals）。

    ★ 归一说明见 ``/trade/orders``。成交无独立成交号，``deal_id`` 由
    ``order_id`` + ``seq`` 兜底（界面要用它做列表 key，必须有值）。
    """
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_deals)
    return envelope_ok(normalize_deals(_enrich_names(res))) if isinstance(res, list) else res

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
    # P0-5：预检口径必须与真实下单一致——实盘（live）才要求账户快照就绪 +
    # 可用资金校验，模拟盘无券商账户、不受该闸门限制。
    _sr = ctx.signal_router
    _live = bool(_sr is not None and getattr(_sr, "mode", "paper") == "live")
    allowed, reason = ctx.risk.precheck_order(code, price, volume, direction, price_type,
                                              require_account=_live)
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

