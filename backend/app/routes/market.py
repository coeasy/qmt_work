# --- stdlib imports injected by fix_route_imports ---
import asyncio
import logging
import os
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.routes._common import BrokerError, _call, _need, envelope_ok, err, no_broker, ok, state
from app.services.market import (
    QUOTES_FILL_SEM,
    ServiceError,
    build_analysis,
    enrich_search_row,
    kline_io,
    normalize_code,
    perf_from_bars,
    perf_stale,
    quote_error,
)
from app.services.market import aggregates as msvc

# 这两个常量定义在 common.py；此前经 aggregates 隐式 re-export 使用，
# 2026-09-08 改为从源头直接导入，消除「删掉 aggregates 的未使用导入就断」的脆弱耦合。
from app.services.market.common import ETF_LIST_TTL, ETF_QUOTE_CAP
from datasource.board import classify_board
from datasource.instrument import with_exchange_suffix
from datasource.periods import (
    UnknownPeriodError,
    UnsupportedPeriodError,
    all_periods,
    normalize_period,
    spec,
    to_eltdx_period,
)
from datasource.registry import (
    DataSourceUnavailable,
    MarketDataUnavailable,
    UnsupportedDataSource,
    get_hub,
)

log = logging.getLogger("qmt_work.market")

router = APIRouter()

# 兼容别名：既有单测/调用方仍以 routes.market 引用（重构 P1-1 保持行为与符号兼容）
_normalize_code = normalize_code
_enrich_search_row = enrich_search_row
_perf_from_bars = perf_from_bars
_perf_stale = perf_stale


@router.get("/market/search")
async def market_search(q: str, limit: int = 20, include_boards: bool = True):
    """标的搜索：代码 / 中文名 / 拼音首字母 / 板块名 模糊匹配（零网络，基于本地缓存）。

    返回 {code:0, data:[{code, name, type, exchange, board, label, match, pinyin?}, ...]}：
    - type: stock/etf/index/board/bond/unknown（画像层分类，前端据此显示徽章）
    - match: exact/code/name/pinyin（命中方式，前端可高亮）
    - include_boards=true（默认）联合板块名称检索，板块条目 type=board
    q 为空时返回空列表。完全离线，不依赖券商连接。
    """
    q = (q or "").strip()
    if not q:
        return ok([])
    limit = max(1, min(int(limit or 20), 50))
    try:
        rows = await get_hub().search_stocks(q, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("股票搜索失败：%s", exc)
        rows = []
    out = [enrich_search_row(r, q) for r in (rows or []) if r.get("code")]
    # 板块联合检索（仅当无股票结果或结果不足时补充；板块排在股票之后）
    if include_boards and len(out) < limit:
        try:
            matches, _ = await get_hub().search_boards(q, max(1, limit - len(out)))
        except Exception:  # noqa: BLE001
            matches = []
        for m in matches or []:
            if len(out) >= limit:
                break
            code = str(m.get("code", "") or "")
            if not code or any(o["code"] == code for o in out):
                continue
            kind = str(m.get("kind", "") or "")
            out.append({"code": code, "name": m.get("name", ""),
                        "type": "board", "exchange": "板块",
                        "board": "行业板块" if kind == "industry" else "概念板块",
                        "label": "板块", "match": "name"})
    return ok(out)


@router.get("/market/resolve")
async def market_resolve(q: str, limit: int = 8):
    """标的解析归一：任意输入（代码/带后缀/前缀式/名称/拼音）→ 标准 QMT 代码 + 候选。

    唯一命中 → resolved=true；多候选 → resolved=false + candidates（前端让用户选择）。
    中文/拼音输入退化为搜索语义。返回
    {q, resolved, code, name, type, exchange, board, label, match, candidates}。
    """
    q = (q or "").strip()
    if not q:
        return err(400, "缺少 q")
    candidates: list = []

    # 1) 代码形式：归一后缀，逐一验证名称表
    for code in normalize_code(q):
        try:
            det = await get_hub().get_instrument_detail(code)
        except Exception:  # noqa: BLE001
            det = None
        name = (det or {}).get("name") or ""
        from datasource.instrument import classify_instrument
        cls = classify_instrument(code, name)
        candidates.append({"code": code, "name": name, "type": cls["type"],
                           "exchange": cls["exchange"], "board": cls["board"],
                           "label": cls["label"], "match": "code"})
    # 代码直接命中唯一候选 → 解析成功
    if len(candidates) == 1 and candidates[0]["name"]:
        c = candidates[0]
        return ok({"q": q, "resolved": True, **c, "candidates": candidates[:limit]})
    # 歧义双候选：取有名称者优先（000001.SH 上证指数 / 000001.SZ 平安银行均合法 → 双候选）

    # 2) 名称/拼音：搜索语义补候选（与 search 同源）
    try:
        rows = await get_hub().search_stocks(q, limit)
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows or []:
        if len(candidates) >= limit:
            break
        code = str(r.get("code", "") or "")
        if not code or any(c["code"] == code for c in candidates):
            continue
        candidates.append(enrich_search_row(r, q))

    resolved = None
    if candidates:
        # 名称精确等于 q 的候选唯一 → 直接解析成功
        exact = [c for c in candidates if c.get("name") and c["name"] == q]
        if len(exact) == 1:
            resolved = exact[0]
        elif len(candidates) == 1 and candidates[0].get("name"):
            resolved = candidates[0]
    if resolved:
        return ok({"q": q, "resolved": True, **resolved,
                   "candidates": candidates[:limit]})
    return ok({"q": q, "resolved": False, "code": "", "name": "",
               "type": "unknown", "exchange": "—", "board": "—", "label": "标的",
               "candidates": candidates[:limit]})


@router.get("/market/analysis")
async def market_analysis(code: str, conn_id: str = "", source: str = "auto"):
    """标的深度画像：单请求并发聚合 6 维（快照/画像/股本/表现/资金流/估值）。

    编排逻辑见 services/market/analysis.py；此处只做参数校验与信封包装。
    """
    if not (code or "").strip():
        return err(400, "缺少 code")
    try:
        return ok(await build_analysis(get_hub(), code, conn_id=conn_id,
                                       source=source, broker_of=_need))
    except ValueError:
        return err(400, "缺少 code")


@router.get("/market/quote")
async def market_quote(code: str, conn_id: str = "", source: str = "auto"):
    """实时行情快照（最新价 / 涨跌幅 / 成交量 / 买卖五档 + 合约名称/涨跌停）。

    source:
      - auto（默认）：券商优先，券商未连接或失败时回退 eltdx(TDX 公共行情)
      - broker：仅券商
      - eltdx：仅 TDX 公共行情（无需券商客户端）
    """
    try:
        q = await get_hub().get_quote(code, source=source, conn_id=conn_id or None)
    except UnsupportedDataSource as exc:
        return err(400, str(exc))
    except MarketDataUnavailable:
        return err(*quote_error(code, source))
    if not q or not isinstance(q, dict) or q.get("last") is None:
        # 彻底无可用源（auto 链全失败 / 显式源缺数据）：绝不静默返回 null，
        # 让前端能据此显示明确的「行情不可用」而非误判为“连接正常但无数据”。
        return err(*quote_error(code, source))
    # 必须用 ok() 包裹：quote dict 自带 "code" 字段（股票代码），若裸返回，
    # 前端 _req 会误判 `j.code !== 0` 直接抛错 → 个股/行情分析全部空数据。
    return ok(q)


@router.post("/market/quotes")
async def market_quotes(body: dict):
    """批量行情快照（报价牌 / 综合排名命脉）。

    优先返回 SyncEngine.latest_quotes 缓存中已订阅的实时快照（零新增网络调用）；
    缓存缺失的代码经 DataSourceManager.get_quote 尽力补齐（单只 5s 超时、整体 gather，
    失败静默跳过），保证报价牌/排名首屏即有数据，不伪造。

    参数（body JSON）：codes(必填) / source / conn_id。
    返回 {items:[归一化 quote...], served, requested}。
    """
    codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    if not codes:
        return ok({"items": []})
    source = str(body.get("source") or "auto")
    conn_id = body.get("conn_id") or None
    m = get_hub()
    se = getattr(state, "sync_engine", None)
    cache = getattr(se, "latest_quotes", None) or {}
    items: list = []
    missing: list = []
    for c in codes:
        q = cache.get(c.upper()) or cache.get(c)
        if q and isinstance(q, dict):
            items.append({**q, "source": q.get("source") or "cache"})
        else:
            missing.append(c)
    if missing:
        # R3（P2-1 漏网）：补齐打源加并发上限，报价牌大清单全 miss 时不打爆 TDX 侧。
        async def _fill(c):
            try:
                async with QUOTES_FILL_SEM:
                    return await asyncio.wait_for(
                        m.get_quote(c, source=source, conn_id=conn_id), timeout=5)
            except Exception:  # noqa: BLE001
                return None
        res = await asyncio.gather(*[_fill(c) for c in missing])
        for _c, q in zip(missing, res):
            if q and isinstance(q, dict):
                items.append(q)
    return ok({"items": items, "served": len(items), "requested": len(codes)})


@router.get("/market/stock-info")
async def market_stock_info(code: str, conn_id: str = "", source: str = "auto"):
    """股票基本信息：名称 / 板块 / 交易所 / 涨跌停 / 昨收（供右侧面板）。

    source: auto（券商优先，失败回退 eltdx）/ broker / eltdx
    """
    # 名称表与券商接口均以带后缀代码为键，裸代码会查不到中文名（界面只剩数字）
    code = with_exchange_suffix((code or "").strip().upper())
    board = classify_board(code)
    info = {
        "code": code,
        "name": code,
        "exchange": board.get("exchange"),
        "board": board.get("board"),
        "high_limit": None,
        "low_limit": None,
        "pre_close": None,
        "industry": "",
        "concepts": [],
    }

    try:
        det = await get_hub().get_instrument_detail(code, source=source, conn_id=conn_id or None)
    except UnsupportedDataSource as exc:
        return err(400, str(exc))
    except MarketDataUnavailable:
        # 全部源不可用：板块按代码前缀推断，绝不伪造数值。
        info["note"] = "未连接券商且 TDX 行情源不可用，板块按代码前缀推断"
        return ok(info)

    info["name"] = det.get("name") or code
    info["exchange"] = det.get("exchange") or info["exchange"]
    info["high_limit"] = det.get("high_limit")
    info["low_limit"] = det.get("low_limit")
    info["pre_close"] = det.get("pre_close")
    info["industry"] = det.get("industry") or ""
    info["concepts"] = det.get("concepts") or []
    info["source"] = det.get("source")
    return ok(info)

@router.get("/market/sources")
async def market_sources():
    """列出已注册行情数据源及其可用性（供前端「数据源」选择 / 健康展示）。

    返回 {sources:[name,...], auto_chain:[...], health:{name:{available,note}}, active:当前auto首源}。
    """
    m = get_hub()
    h = await m.health()
    active = next((n for n in m._auto_chain if h.get(n, {}).get("available")), None)
    return ok({
        "sources": m.list_sources(),
        "source_capabilities": m.describe_sources(),
        "auto_chain": m._auto_chain,
        "health": h,
        "active": active,
    })


@router.get("/market/periods")
async def market_periods():
    """可用周期清单（契约驱动 UI 的数据源）。

    前端周期条据此渲染，并对 supported=false 的周期置灰 + tooltip 显示 reason。
    这样后端新增/下线周期时前端自动跟随，杜绝「点了出别的周期」的静默错误（P0-1）。
    """
    return ok({"periods": all_periods()})


@router.get("/market/kline")
async def market_kline(code: str, period: str = "1d", count: int = 250,
                       conn_id: str = "", force: bool = False, source: str = "auto",
                       adj: str = ""):
    """历史 K 线（C1 本地缓存优先；source: auto=券商优先回退eltdx / broker / eltdx）。
    adj: ''=不复权 / qfq=前复权 / hfq=后复权。券商 get_kline 不支持复权，显式复权时
    走 TDX 复权源，避免静默返回原始价误导用户。

    周期在入口即校验：未知周期返回明确错误，不支持的周期（如季线）返回原因。
    ⚠️ 绝不能静默降级为日线 —— 那会让用户看到错误周期的数据却不自知（P0-1）。
    """
    try:
        period = normalize_period(period)
    except UnknownPeriodError as exc:
        return err(400, str(exc))
    # 分时不是 K 线周期：走独立端点 /market/minutes。这里必须显式拒绝并给出
    # 去向，否则会「通过校验 → 券商返回空 → 静默 0 根」，前端图表空白却无报错。
    if spec(period).kind == "tick":
        return err(400, "分时数据请使用 /market/minutes 端点（K 线端点不支持 tick 周期）")
    try:
        if not spec(period).supported:
            return err(400, f"周期 {spec(period).label} 暂不可用：{spec(period).reason}")
    except UnsupportedPeriodError as exc:  # pragma: no cover - spec() 已归一化
        return err(400, str(exc))

    from tools import fetch_kline_cached
    try:
        res = await fetch_kline_cached(code, period, count,
                                       broker_id=conn_id or None, force=force,
                                       source=source,
                                       adjust=adj or None)
    except UnsupportedDataSource as exc:
        return err(400, str(exc))
    except (BrokerError, DataSourceUnavailable) as exc:
        return err(503, str(exc))
    bars = res.get("bars") or []
    # 彻底无源返回：券商 + eltdx(TDX) 均无数据时，G1-6 先试本地数据仓兜底
    # （stale 明示、as_of 标数据截至时间、降级≠造假）；本地也无数据才 503。
    if not bars and not res.get("source"):
        from datasource.degrade import envelope, local_bars
        dres = local_bars(code, period=period, adjust=adj or "")
        if dres is not None:
            return ok(envelope(
                {"code": code, "period": period, "count": len(dres.results),
                 "cached_at": None, "note": None, "adjust": adj or "",
                 "bars": dres.results}, dres))
        return err(503, f"K 线获取失败：{code} 券商不可用、TDX 行情源无数据且本地数据仓为空，请连接券商或检查网络。")
    # ⚠️ stale 一致性（曾经出现 source=cache_stale 却 stale:false 误导用户）：
    # 当缓存层回退供给「过期缓存」时（kline_cache.get_or_fetch 返回 source=cache_stale），
    # 必须同步把 stale 置 True，并透传 as_of（=cached_at 数据截至时间），绝不静默冒充最新。
    src = res.get("source")
    stale = src == "cache_stale"
    return ok({"code": code, "period": period, "count": len(bars),
               "source": src, "cached_at": res.get("cached_at"),
               "as_of": res.get("cached_at") if stale else None,
               "note": res.get("note"), "adjust": adj or "",
               "stale": stale,
               "bars": bars})

@router.get("/market/minutes")
async def market_minutes(code: str, date: str = "", source: str = "auto"):
    """当日分时（1 分钟）曲线：价格线 + 均价线 + 分钟量，含昨收基准。

    date 为空取最新交易日分时；格式 YYYY-MM-DD 取历史分时。
    数据源：TDX 公共行情（eltdx）；券商 SDK 无分时接口。全源无数据返回 503（零 mock）。
    返回 {code, trading_date, pre_close, open_price, points:[{t,price,avg,volume}]}。
    """
    try:
        res = await get_hub().get_minutes(code, trading_date=date or None, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"分时获取失败：{code}（{exc}）")
    if not res or not res.get("points"):
        return err(503, f"分时获取失败：{code} 暂无 TDX 分时数据（非交易时段或网络不可用）。")
    return ok(res)


@router.get("/market/limitup")
async def market_limitup(sector: str = "沪深A股", min_pct: float = 9.5,
                         only_limit: bool = True, limit: int = 200,
                         sort: str = "change"):
    """涨停板：扫描板块内最新行情，列出涨停（或接近涨停）个股及最新数据，便于快速选股交易。"""
    b = _need()
    if b is None:
        return no_broker()
    try:
        from engines.limitup import scan_limit_up
        rows = await scan_limit_up(b, sector, min_pct, only_limit, limit, sort)
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"sector": sector, "count": len(rows), "rows": rows})

@router.get("/market/breadth")
async def market_breadth():
    """市场广度统计：全市场/板块/主要指数涨跌停家数。"""
    b = _need()
    if b is None:
        return no_broker()
    try:
        from engines.limitup import market_breadth as _mb
        return ok(await _mb(b))
    except BrokerError as exc:
        return err(503, str(exc))


# ---------- 多维行情：指数 / 板块 / ETF / 资金流（编排见 services/market/aggregates.py） ----------

@router.get("/market/indices")
async def market_indices(codes: str = "", source: str = "auto", ttl: int = 3,
                         spark: bool = False, spark_days: int = 20):
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
                        limit: int = 50, source: str = "auto", ttl: int = 10):
    """板块榜单（行业 881xxx / 概念 880xxx / 统计类 stat）。"""
    try:
        return ok(await msvc.boards(kind, sort_by=sort_by, limit=limit,
                                    source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/constituents")
async def market_board_constituents(code: str, limit: int = 100, page: int = 0,
                                    source: str = "auto", ttl: int = 15):
    """板块成分股（真实板块成分，非全市场过滤）。"""
    try:
        return ok(await msvc.board_constituents(code, limit=limit, page=page,
                                                source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/lookup")
async def market_board_lookup(name: str, limit: int = 8, source: str = "auto",
                              ttl: int = 600):
    """板块名称 → 代码匹配（P1-8 深链稳化）。"""
    try:
        return ok(await msvc.board_lookup(name, limit=limit, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/kline")
async def market_board_kline(code: str, period: str = "1d", count: int = 60,
                             source: str = "auto", ttl: int = 60):
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
                      ttl: int = ETF_LIST_TTL):
    """ETF 全市场清单（代码段 51/56/58/15/16）。"""
    try:
        return ok(await msvc.etfs(limit, with_quote=with_quote,
                                  quote_limit=quote_limit, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/moneyflow")
async def market_moneyflow(code: str, source: str = "auto"):
    """个股资金流（真实口径：快照内外盘 + 分钟级买卖力道 + 量比）。"""
    try:
        return ok(await msvc.moneyflow(code, source=source))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/capital")
async def market_capital(codes: str, source: str = "auto", ttl: int = 300):
    """批量流通股本 + 涨跌停价（换手率与涨跌停展示的真实口径来源）。"""
    try:
        return ok(await msvc.capital(codes, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/moneyflow")
async def market_board_moneyflow(code: str, source: str = "auto",
                                 top_n: int = 30, ttl: int = 30):
    """板块资金流：聚合成分股当日主力净流入（外盘-内盘），真实口径。"""
    try:
        return ok(await msvc.board_moneyflow(code, source=source, top_n=top_n, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/rotation")
async def market_rotation(days: int = 5, kind: str = "industry",
                          top_n: int = 40, source: str = "auto"):
    """板块轮动：取板块榜 topN（按 |涨跌幅|），各取日K 计算每日%chg，返回矩阵供热力图。"""
    try:
        return ok(await msvc.rotation(days, kind=kind, top_n=top_n, source=source))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/overview")
async def market_overview(source: str = "auto", ttl: int = 10):
    """市场概览（E3）：统计类板块真实家数 + 主要指数快照 + 宽度趋势 + 两市成交额。"""
    try:
        return ok(await msvc.overview(source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


# ===================== G3 资金流落库 / 回放 / 自动采集 =====================
class _MoneyflowSnapshotReq(BaseModel):
    codes: list = []
    board: str = ""


@router.post("/market/moneyflow/snapshot")
async def market_moneyflow_snapshot(body: _MoneyflowSnapshotReq):
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
               "ts": datetime.now().isoformat(timespec="seconds")})


@router.get("/market/moneyflow/replay")
async def market_moneyflow_replay(code: str, date: str = "", limit: int = 500):
    """资金流回放：取 code 的历史快照序列（按 ts 升序）。date=YYYY-MM-DD 可选过滤某日。"""
    if not code:
        return err(400, "缺少 code")
    from core.db import get_db
    return ok(kline_io.moneyflow_replay(get_db(), code, date=date, limit=limit))


# start_moneyflow_collector 由 services/market/kline_io 提供（main.py lifespan 调用）。
start_moneyflow_collector = kline_io.start_moneyflow_collector


@router.get("/market/kline/sync-status")
async def kline_sync_status():
    """行情缓存定时更新状态（开关/触发时间/最近一次运行），供前端展示与配置。"""
    ms = getattr(state, "market_sync", None)
    if ms is None:
        return ok({"initialized": False})
    rc = getattr(state, "runtime_config", None)
    info = {
        "initialized": True,
        "enabled": ms.enabled,
        "sync_time": ms.sync_time,
        # runtime_config 可写标记（put /config/runtime 用同一 domain key）
        "keys": {
            "enabled": "market.sync.enabled",
            "sync_time": "market.sync.time",
        },
        "last_run": getattr(state, "_market_sync_last", None),
        "config": rc.all().get("market.sync.enabled") if rc else None,
    }
    return ok(info)


@router.get("/market/datasets/snapshots")
async def dataset_snapshots(dataset_id: str = "cn_equity_daily", limit: int = 20):
    """查询历史数据集快照；只返回已落库的版本/校验/质量元数据。"""
    from core.db import get_db
    rows = get_db().query(
        "SELECT id,dataset_id,version,provider_id,batch_id,as_of,coverage_start,"
        "coverage_end,row_count,checksum,quality_state,manifest_json,created_at "
        "FROM dataset_snapshots WHERE dataset_id=? ORDER BY created_at DESC LIMIT ?",
        (dataset_id, max(1, min(int(limit), 200))),
    )
    return ok({"dataset_id": dataset_id, "items": rows})


@router.get("/market/kline/cache")
async def kline_cache_stats():
    """K 线缓存统计（行数/热表·归档/序列数/命中率）。"""
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    return ok(await asyncio.to_thread(state.kline_cache.stats))

@router.delete("/market/kline/cache")
async def kline_cache_clear(code: str = "", period: str = ""):
    """清理 K 线缓存（可按 code / code+period 精确清理）。"""
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    n = await asyncio.to_thread(state.kline_cache.clear, code=code, period=period)
    state.db.audit("admin", "kline_cache.clear", code or "*",
                   {"period": period}, f"deleted={n}")
    return ok({"deleted": n})


# ---------------- 历史 K 线导出到本地指定目录（CSV/JSON） ----------------

@router.post("/market/kline/export")
async def kline_export(body: dict):
    """批量导出历史 K 线到本地指定目录（CSV / JSON）。

    参数（body JSON）：
    - dest_dir: 必填，导出目录（不存在自动创建）
    - codes: 可选，代码列表；省略则导出本地缓存中全部 code×period 序列
    - period: 可选，仅导出该周期
    - count: 每序列导出最近 N 根；0/省略=导出该序列全部根数
    - format: csv | json，默认 csv
    - refresh: false（默认）快速直接用本地缓存导出；true 先回源券商刷新到缓存再导出（需连接券商）
    - conn_id: 指定 broker 连接（refresh 回源用）

    数据来自本地 K 线缓存（KlineCache），"快速"导出完全离线，无网络调用。
    """
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    try:
        out = await kline_io.kline_export(state.kline_cache, body)
    except ValueError as exc:
        return err(400, str(exc))
    state.db.audit("admin", "kline.export", body.get("dest_dir") or "",
                   {"format": body.get("format") or "csv",
                    "refresh": bool(body.get("refresh", False)),
                    "dest_dir": body.get("dest_dir") or ""},
                   f"files={out.get('exported', 0)} rows={out.get('rows', 0)}")
    return ok(out)


@router.get("/market/kline/export")
async def kline_export_read(dest_dir: str, code: str, period: str = "1d",
                            format: str = "csv"):
    """读取本地导出目录中已导出的历史 K 线文件（离线/断线时也可用）。

    直接读磁盘文件，不依赖券商连接；文件不存在返回 404。
    format: csv | json（须与导出时一致）。
    """
    from gateway.kline_cache import KlineCache
    path = KlineCache.file_path(code, period, dest_dir, format)
    if not os.path.exists(path):
        return err(404, f"导出文件不存在：{os.path.basename(path)}"
                        "（请先 POST /market/kline/export 导出）")
    try:
        bars = await asyncio.to_thread(KlineCache.read_export, path, format)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"读取导出文件失败：{exc}")
    return ok({"code": code, "period": period, "format": format,
               "file": path, "count": len(bars), "bars": bars})


# ---------------- 同步全部历史 K 线（日线+周线）到本地指定目录 ----------------

@router.post("/market/kline/sync")
async def kline_sync(body: dict):
    """把一批股票的最新历史 K 线（含日线 1d、周线 1w）同步到本地指定目录。

    流程：确定股票集合 → 逐只回源券商拉取最新 K 线写入本地缓存 → 导出到 dest_dir。
    参数（body JSON）：dest_dir(必填)/codes/sector/periods/count/format/limit/conn_id。
    单只失败不中断整体（errors 列出）。真实行情，缺数据不伪造。
    """
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")

    async def _get_sector_stocks(sector: str, conn_id):
        b = _need(conn_id)
        if b is None:
            raise BrokerError("未连接任何券商客户端：省略 codes 需用板块成分，请先连接券商。")
        return await _call(b, b.gateway.get_sector_stocks, sector) or []

    try:
        out = await kline_io.kline_sync(state.kline_cache, body, _get_sector_stocks)
    except ValueError as exc:
        return err(400, str(exc))
    except BrokerError as exc:
        return err(503, str(exc))
    except LookupError as exc:
        return err(404, str(exc))
    state.db.audit("admin", "kline.sync", body.get("dest_dir") or "",
                   {"format": body.get("format") or "csv",
                    "periods": out["periods"], "count": int(body.get("count") or 250),
                    "codes": out["codes_total"]},
                   f"files={out['files_count']} rows={out['rows']} errors={len(out['errors'])}")
    return ok(out)


# ---------------- 行情爬虫（真实 K 线落库） ----------------

@router.post("/market/crawl")
async def crawl_market(body: dict):
    """创建/提交market / crawl（POST /market/crawl）。"""
    try:
        return ok(await kline_io.crawl_market(body))
    except PermissionError as exc:
        return err(503, str(exc))


# ---------------- LLM 配置（加密存储） ----------------

@router.get("/market/l2")
async def market_l2(code: str, count: int = 100):
    """获取market / l2（GET /market/l2）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_l2_transactions, code, count)
    return envelope_ok(res)


# ---------------- 策略模板库 ----------------
