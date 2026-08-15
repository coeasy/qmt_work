"""模拟盘（Paper Trading）路由：虚拟资金撮合 + 真实行情盯市。

引擎实例由集成方注入 `state.paper_engine`（未注入时统一返回 503）。
"""
from app.routes._common import ok, err, state

from fastapi import APIRouter

router = APIRouter()


def _engine():
    """取模拟盘引擎；未初始化返回 None（state 可能尚未挂载该属性）。"""
    return getattr(state, "paper_engine", None)


_NOT_READY = "模拟盘引擎未初始化"


@router.post("/paper/reset")
async def paper_reset(body: dict | None = None):
    """重置模拟盘：清空持仓/成交并重设初始资金。body: {initial_capital?}"""
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    body = body or {}
    try:
        initial = float(body.get("initial_capital") or 1_000_000.0)
        return ok(e.reset(initial))
    except (TypeError, ValueError) as exc:
        return err(400, str(exc))


@router.post("/paper/order")
async def paper_order(body: dict):
    """模拟下单（立即以给定价格成交）。body: {code, side, price, volume, price_type?, remark?}"""
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    body = body or {}
    try:
        order = e.submit_order(
            code=body.get("code", ""),
            side=body.get("side", ""),
            price=body.get("price", 0),
            volume=body.get("volume", 0),
            price_type=body.get("price_type", "limit"),
            remark=body.get("remark", ""),
        )
    except (TypeError, ValueError) as exc:
        return err(400, str(exc))
    db = getattr(state, "db", None)
    if db is not None:
        try:
            db.audit("paper", "paper.order", order["code"],
                     {"side": order["side"], "price": order["price"],
                      "volume": order["volume"]}, "filled")
        except Exception:  # noqa: BLE001 审计失败不影响模拟撮合
            pass
    return ok(order)


@router.get("/paper/account")
async def paper_account():
    """模拟盘资产：现金 / 市值 / 总资产 / 浮动与已实现盈亏。

    市值与浮动盈亏基于**实时行情**最新价盯市（来自同步引擎行情缓存，不编造价格）。
    """
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    se = getattr(state, "sync_engine", None)
    if se is not None:
        # 从行情缓存抽取最新价（兼容 last/lastPrice/price/close 字段）
        price_map = {}
        for code, q in (getattr(se, "latest_quotes", {}) or {}).items():
            if not isinstance(q, dict):
                continue
            for k in ("last", "lastPrice", "price", "close"):
                v = q.get(k)
                if v:
                    try:
                        price_map[code] = float(v)
                        break
                    except (TypeError, ValueError):
                        continue
        e.sync_from_map(price_map)
    return ok(e.get_account())


@router.get("/paper/positions")
async def paper_positions():
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    return ok(e.get_positions())


@router.get("/paper/trades")
async def paper_trades(limit: int = 50):
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    return ok(e.get_trades(limit))


@router.get("/paper/metrics")
async def paper_metrics():
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    return ok(e.metrics())
