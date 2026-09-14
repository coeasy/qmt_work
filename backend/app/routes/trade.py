import asyncio
import logging

from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import (MSG_NO_BROKER, _call, _need, envelope_ok, err,
                                no_broker, ok)
from app.services.market.common import QUOTES_FILL_SEM, TTLCache

# --- stdlib imports injected by fix_route_imports ---
from datasource.registry import get_manager
from gateway.execution import get_execution_service
from gateway.idempotency import single_flight

log = logging.getLogger("qmt_work.routes.trade")


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


# ---------------- 持仓行情补算（现价 / 盈亏 / 盈亏比） ----------------

# 单只补齐的超时与并发上限：与 /market/quotes 同源限流，避免持仓页轮询打爆行情侧。
# 超时压到 2.5s：真实环境里 broker quote 的合约详情富化会回落到不可达的公共源
# （实测单次约 5.6s），持仓页不该为此干等——拿不到就走负缓存快速降级，
# 由 SyncEngine 持仓订阅（零打源）在秒级内把现价补上。
_POS_QUOTE_TIMEOUT = 2.5
# 现价短 TTL 缓存：持仓页会被前端定时轮询，同代码 5s 内复用同一结果，
# 既挡住轮询风暴，又保证「现价」不因缓存而明显滞后（行情本身为秒级快照）。
_POS_PRICE_TTL = 5.0
# 负缓存：确证「拿不到」后短时间内不再重复打源（否则每次轮询都白等一个超时）。
_POS_PRICE_FAIL_TTL = 20.0
_NO_PRICE = object()
_POS_PRICE_CACHE = TTLCache(max_entries=4096, keep=3800, hard_ttl=120)


def _pick_last_price(q) -> float | None:
    """从行情快照 dict 提取最新价（多源键名兼容；非正数/不可解析视为缺失，不伪造）。

    hub 归一化后的 quote 以 ``last`` 为准（见 datasource/registry._merge_quote），
    但券商/第三方源历史字段名不一，这里做兼容读取。停牌 last==0 视为缺失。
    """
    if not isinstance(q, dict):
        return None
    for k in ("last", "price", "lastPrice", "close"):
        v = q.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def _apply_pnl(rows):
    """按 现价 / 成本 / 数量 计算浮动盈亏与盈亏比（数据不全则留 None，绝不伪造）。

    - profit     = (price - cost) × volume
    - profit_pct = (price - cost) / cost × 100

    成本字段多版本兼容：券商契约是 ``cost``（xtquant_client/xtp/account.py:55），
    但历史适配器/模拟盘用 ``avg_cost`` / ``cost_price``。此处兼容读取并**归一化**
    到 ``cost``（前端持仓「成本」列只认 ``cost``，否则该列同样会空）。
    券商已给 profit/market_value 时不覆盖（以券商口径为准）。
    """
    for r in rows:
        if not isinstance(r, dict):
            continue
        price = _pick_last_price(r)
        # 成本归一化：多版本键名 → 统一写回 cost
        cost = r.get("cost")
        if cost is None:
            for k in ("avg_cost", "cost_price", "open_price"):
                if r.get(k) is not None:
                    cost = r[k]
                    r["cost"] = cost
                    break
        try:
            cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        try:
            vol = float(r.get("volume")) if r.get("volume") is not None else None
        except (TypeError, ValueError):
            vol = None
        if price is not None and cost and cost > 0 and vol:
            if r.get("profit") is None:
                r["profit"] = round((price - cost) * vol, 2)
            if r.get("profit_pct") is None:
                r["profit_pct"] = round((price - cost) / cost * 100, 2)
        # 市值缺失时用 现价×数量 兜底（券商已给则不动，尊重券商口径）
        if price is not None and not r.get("market_value") and vol:
            r["market_value"] = round(price * vol, 2)
    return rows


async def _enrich_positions(rows, ctx, b=None):
    """给券商持仓行补算 现价 / 盈亏 / 盈亏比，并兜底补全中文名。

    背景（零 mock 契约）：券商 ``get_positions`` 只回 code/name/volume/avail/cost/
    market_value（权威定义 xtquant_client/xtp/account.py:55），**不含现价与浮动盈亏**，
    于是前端持仓表「现价 / 盈亏 / 盈亏比」三列恒为空。这里按真实行情补算：

      1) 先查 SyncEngine.latest_quotes 订阅缓存（零网络调用，命中即用）；
      2) 未命中走**券商直连快照**（b.gateway.get_quote，只取 last，不做画像富化）；
      3) 再兜底 hub.get_quote（限流 + 2.5s 超时 + 正/负短 TTL 缓存）。

    第 2 步不可省：hub 的 broker 路径会做「合约详情富化」，详情为空壳时回落到
    公共源（实测 sina 403 经代理耗时约 5.6s），持仓页不可能等这个延迟。

    任何异常都不得阻断持仓返回（持仓本身来自券商，是本接口的核心数据）。
    """
    rows = _enrich_names(rows)
    if not isinstance(rows, list) or not rows:
        return rows
    try:
        await _fill_position_prices(rows, ctx, b)
    except Exception as exc:  # noqa: BLE001
        # 补算是增强项，失败降级为「只有持仓、无现价」，不影响主数据链路。
        log.debug("持仓行情补算失败（已降级）：%s", exc)
    return _apply_pnl(rows)


async def _fill_position_prices(rows, ctx, b=None) -> None:
    """把可得的现价写回 rows[*]['price']（缓存优先，未命中再打源）。"""
    want: list[str] = []
    for r in rows:
        if not isinstance(r, dict) or _pick_last_price(r) is not None:
            continue
        code = r.get("code") or r.get("stock_code") or r.get("symbol") or ""
        if code:
            want.append(str(code).upper())
    if not want:
        return

    cache = getattr(getattr(ctx, "sync_engine", None), "latest_quotes", None) or {}
    prices: dict[str, float] = {}
    miss: list[str] = []
    for c in dict.fromkeys(want):
        bare = c.split(".")[0]
        # ① SyncEngine 订阅缓存最实时且零网络（键通常带交易所后缀，兼容裸代码）
        p = _pick_last_price(cache.get(c)) or _pick_last_price(cache.get(bare))
        if p is None:
            # ② 负缓存：近期已确证拿不到行情 → 跳过打源（不伪造，只是不再空等）
            if _POS_PRICE_CACHE.get(c, _POS_PRICE_FAIL_TTL) is _NO_PRICE:
                continue
            # ③ 打源结果的短 TTL 复用（挡轮询风暴，但不得盖过实时 tick）
            p = _POS_PRICE_CACHE.get(c, _POS_PRICE_TTL)
        if p is not None:
            prices[c] = p
        else:
            miss.append(c)

    # ③ 券商直连快照：绕过 hub 的画像富化（后者可能被不可达公共源拖到秒级）
    if miss and b is not None:
        async def _from_broker(c: str):
            try:
                q = await _call(b, b.gateway.get_quote, c, timeout=_POS_QUOTE_TIMEOUT)
            except Exception:  # noqa: BLE001
                return c, None
            return c, _pick_last_price(q)

        for c, p in await asyncio.gather(*[_from_broker(c) for c in miss]):
            if p is not None:
                prices[c] = p
                _POS_PRICE_CACHE.set(c, p)
        miss = [c for c in miss if c not in prices]

    # ④ hub 兜底（best-effort：失败静默跳过，不阻断持仓返回）
    if miss:
        try:
            hub = get_manager()
        except Exception:  # noqa: BLE001
            hub = None
        if hub is not None:
            async def _fill(c: str):
                try:
                    async with QUOTES_FILL_SEM:
                        q = await asyncio.wait_for(
                            hub.get_quote(c, source="auto"), timeout=_POS_QUOTE_TIMEOUT)
                except Exception:  # noqa: BLE001
                    return c, None
                return c, _pick_last_price(q)

            for c, p in await asyncio.gather(*[_fill(c) for c in miss]):
                if p is not None:
                    prices[c] = p
                    _POS_PRICE_CACHE.set(c, p)
                else:
                    _POS_PRICE_CACHE.set(c, _NO_PRICE)

    # 回填：无论来源是缓存还是打源，都必须写回（此前缓存全命中时提前 return 会漏写）
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("code") or r.get("stock_code") or r.get("symbol") or "").upper()
        p = prices.get(code)
        if p is not None and _pick_last_price(r) is None:
            r["price"] = p


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
    补算 price / profit / profit_pct（拿不到行情则留空，见 _enrich_positions）。
    """
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_positions, symbol or None)
    return (envelope_ok(await _enrich_positions(res, ctx, b))
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

