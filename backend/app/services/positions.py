"""持仓行富化服务：中文名兜底 + 现价/盈亏/盈亏比补算。

为什么需要（零 mock 契约）：券商 ``get_positions`` 只回
``code / name / volume / avail / cost / market_value``
（权威定义 ``xtquant_client/xtp/account.py:55``），**不含现价与浮动盈亏**。
而前端多处都按「持仓行自带 profit」的口径聚合：

- ``/trade/positions`` → 委托/持仓/成交页、手动交易页（现价/盈亏/盈亏比三列）
- ``/account/status``  → 仪表盘「持仓盈亏」（对 positions 求和 ``p.profit``）

所以补算必须是**一份实现、两处复用**，否则两页会各说各话
（实测踩到：持仓页显示 -10.4，仪表盘同时显示 0）。本模块即该唯一实现，
原先是 ``app/routes/trade.py`` 里的私有函数，抽出来供两个路由共用。

取价优先级（顺序是踩坑后才定下来的，见 ``_fill_position_prices``）：
① SyncEngine 订阅缓存 → ② 短 TTL 复用 → ③ 券商直连快照 → ④ hub 兜底。
"""
import asyncio
import logging

from datasource.registry import get_manager

from app.services.market.common import QUOTES_FILL_SEM, TTLCache

log = logging.getLogger("qmt_work.services.positions")

# 单只补齐的超时与并发上限：与 /market/quotes 同源限流，避免持仓页轮询打爆行情侧。
# 超时压到 2.5s：真实环境里 broker quote 的合约详情富化会回落到不可达的公共源
# （实测单次约 5.6s），持仓页不该为此干等——拿不到就走负缓存快速降级，
# 由 SyncEngine 持仓订阅（零打源）在秒级内把现价补上。
_QUOTE_TIMEOUT = 2.5
# 现价短 TTL 缓存：持仓页会被前端定时轮询，同代码 5s 内复用同一结果，
# 既挡住轮询风暴，又保证「现价」不因缓存而明显滞后（行情本身为秒级快照）。
_PRICE_TTL = 5.0
# 负缓存：确证「拿不到」后短时间内不再重复打源（否则每次轮询都白等一个超时）。
_PRICE_FAIL_TTL = 20.0
_NO_PRICE = object()
_PRICE_CACHE = TTLCache(max_entries=4096, keep=3800, hard_ttl=120)


def clear_price_cache() -> None:
    """清空现价缓存。

    模块级缓存跨请求/跨用例共享，测试必须在用例间清空，否则上一条流程写入的值
    会喂给下一条（实测表现为「注入 1688 却读到上条流程的 12.0」的伪失败）。
    """
    _PRICE_CACHE._data.clear()


def pick_last_price(q) -> float | None:
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


def enrich_names(rows):
    """用本地名称表 O(1) 兜底富化券商返回的裸代码 name（持仓/委托/成交常只剩代码）。

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


def apply_pnl(rows):
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
        price = pick_last_price(r)
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


async def enrich_positions(rows, ctx, bridge=None):
    """给券商持仓行补算 现价 / 盈亏 / 盈亏比，并兜底补全中文名。

    取价优先级与取舍见 ``_fill_position_prices``。任何异常都不得阻断持仓返回
    （持仓本身来自券商，是调用方的核心数据）。
    """
    rows = enrich_names(rows)
    if not isinstance(rows, list) or not rows:
        return rows
    try:
        await _fill_position_prices(rows, ctx, bridge)
    except Exception as exc:  # noqa: BLE001
        # 补算是增强项，失败降级为「只有持仓、无现价」，不影响主数据链路。
        log.debug("持仓行情补算失败（已降级）：%s", exc)
    return apply_pnl(rows)


async def _broker_last_price(bridge, code: str) -> float | None:
    """券商直连快照取最新价（只取 last，**不做**合约画像富化）。

    第 ③ 步不可省：hub 的 broker 路径会做「合约详情富化」，详情为空壳时回落到
    公共源（实测 sina 403 经代理耗时约 5.6s），持仓页不可能等这个延迟。
    """
    try:
        q = await asyncio.wait_for(
            bridge.call(bridge.gateway.get_quote, code), timeout=_QUOTE_TIMEOUT)
    except Exception:  # noqa: BLE001
        return None
    return pick_last_price(q)


async def _fill_position_prices(rows, ctx, bridge=None) -> None:
    """把可得的现价写回 rows[*]['price']（缓存优先，未命中再打源）。"""
    want: list[str] = []
    for r in rows:
        if not isinstance(r, dict) or pick_last_price(r) is not None:
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
        p = pick_last_price(cache.get(c)) or pick_last_price(cache.get(bare))
        if p is None:
            # ② 负缓存：近期已确证拿不到行情 → 跳过打源（不伪造，只是不再空等）
            if _PRICE_CACHE.get(c, _PRICE_FAIL_TTL) is _NO_PRICE:
                continue
            # ③ 打源结果的短 TTL 复用（挡轮询风暴，但不得盖过实时 tick）
            p = _PRICE_CACHE.get(c, _PRICE_TTL)
        if p is not None:
            prices[c] = p
        else:
            miss.append(c)

    # ④ 券商直连快照：绕过 hub 的画像富化（后者可能被不可达公共源拖到秒级）
    if miss and bridge is not None:
        for c, p in zip(miss, await asyncio.gather(
                *[_broker_last_price(bridge, c) for c in miss])):
            if p is not None:
                prices[c] = p
                _PRICE_CACHE.set(c, p)
        miss = [c for c in miss if c not in prices]

    # ⑤ hub 兜底（best-effort：失败静默跳过，不阻断持仓返回）
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
                            hub.get_quote(c, source="auto"), timeout=_QUOTE_TIMEOUT)
                except Exception:  # noqa: BLE001
                    return c, None
                return c, pick_last_price(q)

            for c, p in await asyncio.gather(*[_fill(c) for c in miss]):
                if p is not None:
                    prices[c] = p
                    _PRICE_CACHE.set(c, p)
                else:
                    _PRICE_CACHE.set(c, _NO_PRICE)

    # 回填：无论来源是缓存还是打源，都必须写回
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("code") or r.get("stock_code") or r.get("symbol") or "").upper()
        p = prices.get(code)
        if p is not None and pick_last_price(r) is None:
            r["price"] = p
