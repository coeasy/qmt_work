from core.context import AppContext, get_ctx, active_context
"""模拟盘（Paper Trading）路由：虚拟资金撮合 + 真实行情盯市。

引擎实例由集成方注入 `state.paper_engine`（未注入时统一返回 503）。
"""
import logging

from fastapi import APIRouter, Depends

from app.routes._common import err, ok

log = logging.getLogger("qmt_work.routes.paper")

router = APIRouter()


def _engine():
    """取模拟盘引擎；未初始化返回 None（active_context() 可能尚未挂载该属性）。"""
    return getattr(active_context(), "paper_engine", None)


_NOT_READY = "模拟盘引擎未初始化"


@router.post("/paper/reset")
async def paper_reset(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
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
async def paper_order(body: dict, ctx: AppContext = Depends(get_ctx)):
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
    db = getattr(ctx, "db", None)
    if db is not None:
        try:
            db.audit("paper", "paper.order", order["code"],
                     {"side": order["side"], "price": order["price"],
                      "volume": order["volume"]}, "filled")
        except (AttributeError, OSError, RuntimeError) as exc:
            # 审计失败不影响模拟撮合：仅丢一条审计记录
            log.debug("paper 审计写入失败（已忽略）：%s", exc)
    return ok(order)


@router.get("/paper/account")
async def paper_account(ctx: AppContext = Depends(get_ctx)):
    """模拟盘资产：现金 / 市值 / 总资产 / 浮动与已实现盈亏。

    市值与浮动盈亏基于**实时行情**最新价盯市（来自同步引擎行情缓存，不编造价格）。
    """
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    se = getattr(ctx, "sync_engine", None)
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
async def paper_positions(ctx: AppContext = Depends(get_ctx)):
    """获取paper / positions（GET /paper/positions）。"""
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    return ok(e.get_positions())


@router.get("/paper/trades")
async def paper_trades(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """获取paper / trades（GET /paper/trades）。"""
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    return ok(e.get_trades(limit))


@router.get("/paper/metrics")
async def paper_metrics(ctx: AppContext = Depends(get_ctx)):
    """获取paper / metrics（GET /paper/metrics）。"""
    e = _engine()
    if e is None:
        return err(503, _NOT_READY)
    return ok(e.metrics())
