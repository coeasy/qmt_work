"""多维行情子路由：指数 / 板块 / ETF / 资金流（P1-1 自 ``market.py`` 拆出）。

为什么单独成模块
----------------
``market.py`` 曾达 53.8KB，把三类互不相关的关注点混在一个文件里：

1. **单标的行情**（quote / stock-info / kline / minutes）——直连数据源；
2. **缓存与运维**（sync-status / coverage / cache / export）——面向运维面板；
3. **多维聚合**（指数条 / 板块榜 / ETF 清单 / 资金流 / 轮动）——全部经
   ``services/market/aggregates.py`` 编排。

本模块只承载第 3 类：它们形态高度一致（``try`` → ``ok`` / ``ServiceError`` → ``err``），
与第 1 类不共享任何模块级状态。

拆分方式是**子路由挂载**：本模块自带 ``router``，由 ``market.py``
``include_router`` 在**原位置**挂上 —— 对外 URL 与 OpenAPI 端点清单完全不变
（``scripts/ci_reconcile.py`` 的 rest_endpoints 契约不受影响）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.routes._common import err, ok
from app.services.market import ServiceError, kline_io
from app.services.market import aggregates as msvc
from app.services.market.common import ETF_LIST_TTL, ETF_QUOTE_CAP
from core.clock import now_iso
from core.context import AppContext, get_ctx
from datasource.periods import to_eltdx_period
from datasource.registry import get_hub

log = logging.getLogger("qmt_work.market.multidim")

router = APIRouter()

# ---------- 多维行情：指数 / 板块 / ETF / 资金流（编排见 services/market/aggregates.py） ----------

@router.get("/market/indices")
async def market_indices(codes: str = "", source: str = "auto", ttl: int = 3,
                         spark: bool = False, spark_days: int = 20, ctx: AppContext = Depends(get_ctx)):
    """主要指数聚合快照（顶部指数条数据源）。

    并发拉取，单只失败返回 null 并计入 errors，不因一只失败拖垮整条。
    codes: 逗号分隔，默认 DEFAULT_INDICES。ttl: 秒级缓存（0=不缓存）。
    spark=true 时附带近 spark_days 日收盘价序列（真实 K 线，kind=index）。
    """
    try:
        return ok(await msvc.indices_snapshot(codes, source=source, ttl=ttl,
                                              spark=spark, spark_days=spark_days))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/boards")
async def market_boards(kind: str = "industry", sort_by: str = "pct",
                        limit: int = 50, source: str = "auto", ttl: int = 10, ctx: AppContext = Depends(get_ctx)):
    """板块榜单（行业 881xxx / 概念 880xxx / 统计类 stat）。"""
    try:
        return ok(await msvc.boards(kind, sort_by=sort_by, limit=limit,
                                    source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/constituents")
async def market_board_constituents(code: str, limit: int = 100, page: int = 0,
                                    source: str = "auto", ttl: int = 15, ctx: AppContext = Depends(get_ctx)):
    """板块成分股（真实板块成分，非全市场过滤）。"""
    try:
        return ok(await msvc.board_constituents(code, limit=limit, page=page,
                                                source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/lookup")
async def market_board_lookup(name: str, limit: int = 8, source: str = "auto",
                              ttl: int = 600, ctx: AppContext = Depends(get_ctx)):
    """板块名称 → 代码匹配（P1-8 深链稳化）。"""
    try:
        return ok(await msvc.board_lookup(name, limit=limit, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/kline")
async def market_board_kline(code: str, period: str = "1d", count: int = 60,
                             source: str = "auto", ttl: int = 60, ctx: AppContext = Depends(get_ctx)):
    """板块 / 指数 K 线（内部按 kind=index 取，避免 ProtocolError）。"""
    try:
        # 未知周期 UnknownPeriodError / 数据源不支持 UnsupportedPeriodError，
        # 二者均为 ValueError 子类；绝不静默降级为日线。
        to_eltdx_period(period)
    except ValueError as exc:
        return err(400, str(exc))
    try:
        return ok(await msvc.board_kline(code, period=period, count=count,
                                         source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/etfs")
async def market_etfs(limit: int = 0, with_quote: bool = False,
                      quote_limit: int = ETF_QUOTE_CAP, source: str = "auto",
                      ttl: int = ETF_LIST_TTL, ctx: AppContext = Depends(get_ctx)):
    """ETF 全市场清单（代码段 51/56/58/15/16）。"""
    try:
        return ok(await msvc.etfs(limit, with_quote=with_quote,
                                  quote_limit=quote_limit, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/moneyflow")
async def market_moneyflow(code: str, source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """个股资金流（真实口径：快照内外盘 + 分钟级买卖力道 + 量比）。"""
    try:
        return ok(await msvc.moneyflow(code, source=source))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/capital")
async def market_capital(codes: str, source: str = "auto", ttl: int = 300, ctx: AppContext = Depends(get_ctx)):
    """批量流通股本 + 涨跌停价（换手率与涨跌停展示的真实口径来源）。"""
    try:
        return ok(await msvc.capital(codes, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/moneyflow")
async def market_board_moneyflow(code: str, source: str = "auto",
                                 top_n: int = 30, ttl: int = 30, ctx: AppContext = Depends(get_ctx)):
    """板块资金流：聚合成分股当日主力净流入（外盘-内盘），真实口径。"""
    try:
        return ok(await msvc.board_moneyflow(code, source=source, top_n=top_n, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/rotation")
async def market_rotation(days: int = 5, kind: str = "industry",
                          top_n: int = 40, source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """板块轮动：取板块榜 topN（按 |涨跌幅|），各取日K 计算每日%chg，返回矩阵供热力图。"""
    try:
        return ok(await msvc.rotation(days, kind=kind, top_n=top_n, source=source))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/overview")
async def market_overview(source: str = "auto", ttl: int = 60, ctx: AppContext = Depends(get_ctx)):
    """市场概览（E3）：统计类板块真实家数 + 主要指数快照 + 宽度趋势 + 两市成交额。

    ttl 默认 60s（原 10s）：本端点三块取数合计冷启动可达数秒、稳态 2.4~4s（实测见
    `services/market/aggregates.py::overview`），而返回内容里唯一会「过期」的指数
    最新价由前端 `useLiveQuotes` 实时叠加，不依赖本快照 ⇒ 短 ttl 只是让用户反复白等。
    调用方仍可显式传 `ttl=0` 强制不走缓存。
    """
    try:
        return ok(await msvc.overview(source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


# ===================== G3 资金流落库 / 回放 / 自动采集 =====================
class _MoneyflowSnapshotReq(BaseModel):
    codes: list = []
    board: str = ""


@router.post("/market/moneyflow/snapshot")
async def market_moneyflow_snapshot(body: _MoneyflowSnapshotReq, ctx: AppContext = Depends(get_ctx)):
    """采集个股/板块资金流快照落库（G3）。body: {codes:[...]} 或 {board:"881319.SH"}。
    返回 {inserted, codes, ts}。"""
    codes = [str(c).strip() for c in (body.codes or []) if c][:300]
    if body.board and not codes:
        try:
            res, _ = await get_hub().get_board_constituents(body.board, 200, 0, source="auto")
            if res and res.get("items"):
                codes = [c["code"] for c in res["items"] if c.get("code")][:300]
        except Exception as exc:  # noqa: BLE001
            return err(503, f"板块成分获取失败：{exc}")
    if not codes:
        return err(400, "缺少 codes 或 board")
    inserted = await kline_io.snapshot_codes(codes)
    return ok({"inserted": inserted, "codes": codes,
               "ts": now_iso()})


@router.get("/market/moneyflow/replay")
async def market_moneyflow_replay(code: str, date: str = "", limit: int = 500, ctx: AppContext = Depends(get_ctx)):
    """资金流回放：取 code 的历史快照序列（按 ts 升序）。date=YYYY-MM-DD 可选过滤某日。"""
    if not code:
        return err(400, "缺少 code")
    if ctx.db is None:
        return err(503, "数据库未初始化")
    return ok(kline_io.moneyflow_replay(ctx.db, code, date=date, limit=limit))
