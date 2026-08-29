from app.routes._common import ok, err, state, _need, _call, BrokerError

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import json
import logging
import os
import time
from datetime import datetime

from app.datasource.board import classify_board
from app.datasource.manager import get_hub, MarketDataUnavailable
from app.db import get_db
from pydantic import BaseModel

log = logging.getLogger("qmt_work.market")

# R3：/market/quotes 缓存补齐打源并发上限（报价牌大清单全 miss 时不打爆 TDX 侧）。
_QUOTES_FILL_SEM = asyncio.Semaphore(8)


router = APIRouter()


def _quote_err(code: str, source: str):
    """按 source 返回清晰的行情不可用错误（取代静默 null）。"""
    if source == "eltdx":
        return err(503, f"TDX 行情源不可用：{code}（请检查网络，或连接券商获取更稳定行情）")
    if source == "broker":
        return err(503, f"行情获取失败：券商连接异常或未连接，无法取到 {code} 行情。")
    return err(503, f"行情获取失败：{code} 券商不可用且 TDX 行情源亦无数据，请连接券商或检查网络。")


@router.get("/market/search")
async def market_search(q: str, limit: int = 20):
    """股票搜索：按中文名 / 代码模糊匹配（零网络，基于本地名称缓存）。

    返回 [{"code": "600519.SH", "name": "贵州茅台"}, ...]，最多 limit 条。
    q 为空时返回空列表。完全离线，不依赖券商连接。
    """
    q = (q or "").strip()
    if not q:
        return []
    try:
        return await get_hub().search_stocks(q, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("股票搜索失败：%s", exc)
        return []


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
    except MarketDataUnavailable:
        return _quote_err(code, source)
    if not q or not isinstance(q, dict) or q.get("last") is None:
        # 彻底无可用源（auto 链全失败 / 显式源缺数据）：绝不静默返回 null，
        # 让前端能据此显示明确的「行情不可用」而非误判为“连接正常但无数据”。
        return _quote_err(code, source)
    # 必须用 ok() 包裹：quote dict 自带 "code" 字段（股票代码），若裸返回，
    # 前端 _req 会误判 `j.code !== 0` 直接抛错 → 个股/行情分析全部空数据。
    return ok(q)


@router.post("/market/quotes")
async def market_quotes(body: dict):
    """批量行情快照（报价牌 / 综合排名命脉）。

    优先返回 SyncEngine.latest_quotes 缓存中已订阅的实时快照（零新增网络调用）；
    缓存缺失的代码经 DataSourceManager.get_quote 尽力补齐（单只 5s 超时、整体 gather，
    失败静默跳过），保证报价牌/排名首屏即有数据，不伪造。

    参数（body JSON）：
    - codes: 代码列表（必填）
    - source: auto(默认, 券商优先回退补充源) / broker / eltdx
    - conn_id: 指定券商连接

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
                async with _QUOTES_FILL_SEM:
                    return await asyncio.wait_for(
                        m.get_quote(c, source=source, conn_id=conn_id), timeout=5)
            except Exception:  # noqa: BLE001
                return None
        res = await asyncio.gather(*[_fill(c) for c in missing])
        for c, q in zip(missing, res):
            if q and isinstance(q, dict):
                items.append(q)
    return ok({"items": items, "served": len(items), "requested": len(codes)})


@router.get("/market/stock-info")
async def market_stock_info(code: str, conn_id: str = "", source: str = "auto"):
    """股票基本信息：名称 / 板块 / 交易所 / 涨跌停 / 昨收（供右侧面板）。

    source: auto（券商优先，失败回退 eltdx）/ broker / eltdx
    """
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
    from app.datasource.periods import all_periods
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
    from app.datasource.periods import (
        UnknownPeriodError,
        UnsupportedPeriodError,
        normalize_period,
        spec,
    )
    try:
        period = normalize_period(period)
    except UnknownPeriodError as exc:
        return err(400, str(exc))
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
    except BrokerError as exc:
        return err(503, str(exc))
    bars = res.get("bars") or []
    # 彻底无源返回：券商 + eltdx(TDX) 均无数据时才报错，避免前端把「行情不可用」误当空表。
    if not bars and not res.get("source"):
        return err(503, f"K 线获取失败：{code} 券商不可用且 TDX 行情源亦无数据，请连接券商或检查网络。")
    return ok({"code": code, "period": period, "count": len(bars),
               "source": res.get("source"), "cached_at": res.get("cached_at"),
               "note": res.get("note"), "adjust": adj or "",
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
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    try:
        from tools.limitup import scan_limit_up
        rows = await scan_limit_up(b, sector, min_pct, only_limit, limit, sort)
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"sector": sector, "count": len(rows), "rows": rows})

@router.get("/market/breadth")
async def market_breadth():
    """市场广度统计：全市场/板块/主要指数涨跌停家数。"""
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    try:
        from tools.limitup import market_breadth as _mb
        return ok(await _mb(b))
    except BrokerError as exc:
        return err(503, str(exc))


# ---------- 多维行情：指数 / 板块 / ETF / 资金流 ----------
# J1 轻量 TTL 缓存：多窗口同时打开指数条/板块榜时不重复打源。
# 只缓存"清单类/快照类"高频且可接受秒级延迟的数据；个股 quote/K线 走既有链路不缓存。
_TTL_CACHE: dict = {}
_TTL_CACHE_MAX = 512    # 容量上限：达到即触发淘汰（R1）
_TTL_CACHE_KEEP = 480   # 淘汰后保留的水位
_TTL_HARD_TTL = 3600    # 过期残留回收：写入超过 1h 的键视为死键（各端点 ttl 均远小于此）


def _ttl_get(key: str, ttl: float):
    hit = _TTL_CACHE.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _ttl_set(key: str, val) -> None:
    # R1：缓存加上限，防长跑慢泄漏（capital key 含多代码组合可无限增长）。
    now = time.time()
    if len(_TTL_CACHE) >= _TTL_CACHE_MAX:
        # 先清理全部过期键（ttl_get 只查单个 key，过期残留靠这里回收）
        for k in [k for k, (ts, _) in _TTL_CACHE.items() if now - ts >= _TTL_HARD_TTL]:
            _TTL_CACHE.pop(k, None)
        # 仍超限则按「最旧写入时间」淘汰至 _TTL_CACHE_KEEP
        if len(_TTL_CACHE) >= _TTL_CACHE_MAX:
            oldest = sorted(_TTL_CACHE.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[: len(_TTL_CACHE) - _TTL_CACHE_KEEP]:
                _TTL_CACHE.pop(k, None)
    _TTL_CACHE[key] = (now, val)


DEFAULT_INDICES = [
    "000001.SH",  # 上证指数
    "399001.SZ",  # 深证成指
    "399006.SZ",  # 创业板指
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000016.SH",  # 上证50
    "000688.SH",  # 科创50
    "899050.BJ",  # 北证50
]


def _configured_indices() -> list:
    """R8/B4：指数清单 runtime_config 化。

    读 `market.indices.list`（逗号分隔 QMT 代码），留空/非法回退内置 DEFAULT_INDICES，
    改清单经 PUT /config/runtime 即可热生效，无需改代码重发版。
    """
    try:
        rc = state.runtime_config
        raw = (rc.get("market.indices.list") or "") if rc else ""
    except Exception:  # noqa: BLE001
        raw = ""
    conf = [c.strip() for c in str(raw).replace("，", ",").split(",") if c.strip()]
    return conf or DEFAULT_INDICES

_INDICES_SEM = asyncio.Semaphore(6)   # 指数快照 / spark 并发上限（避免连接风暴）


@router.get("/market/indices")
async def market_indices(codes: str = "", source: str = "auto", ttl: int = 3,
                         spark: bool = False, spark_days: int = 20):
    """主要指数聚合快照（顶部指数条数据源）。

    并发拉取，单只失败返回 null 并计入 errors，不因一只失败拖垮整条。
    codes: 逗号分隔，默认 DEFAULT_INDICES。ttl: 秒级缓存（0=不缓存）。
    spark=true 时附带近 spark_days 日收盘价序列（真实 K 线，kind=index）。
    返回 {items:[quote|null...], codes, groups?, errors, source, ts}。
    """
    want = [c.strip() for c in (codes or "").split(",") if c.strip()] or _configured_indices()
    ck = f"indices:{','.join(want)}:{source}:{int(bool(spark))}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    m = get_hub()

    async def _one(c):
        async with _INDICES_SEM:
            try:
                return c, await asyncio.wait_for(m.get_quote(c, source=source), timeout=6), None
            except asyncio.TimeoutError:
                return c, None, "超时（6s）"
            except Exception as exc:  # noqa: BLE001
                return c, None, str(exc)

    res = await asyncio.gather(*[_one(c) for c in want])
    items, errors = [], {}
    for c, q, e in res:
        items.append(q)
        if e:
            errors[c] = e
    spark_map = {}
    if spark:
        n = max(5, min(int(spark_days or 20), 60))

        async def _sp(c):
            async with _INDICES_SEM:
                try:
                    bars, _ = await asyncio.wait_for(
                        m.get_board_kline(c, "1d", n, source=source), timeout=8)
                    return c, [float(b.get("close")) for b in (bars or []) if b.get("close")]
                except Exception:  # noqa: BLE001
                    return c, []

        for c, sp in await asyncio.gather(*[_sp(c) for c in want]):
            if sp:
                spark_map[c] = sp
        for i, q in enumerate(items):
            if q:
                q["spark"] = spark_map.get(want[i]) or None
    out = {"items": items, "codes": want, "errors": errors,
           "ts": datetime.now().isoformat(timespec="seconds"),
           "source": source}
    if ttl > 0 and items and any(items):
        _ttl_set(ck, out)
    return ok(out)


@router.get("/market/boards")
async def market_boards(kind: str = "industry", sort_by: str = "pct",
                        limit: int = 50, source: str = "auto", ttl: int = 10):
    """板块榜单（行业 881xxx / 概念 880xxx / 统计类 stat）。

    走真实板块指数快照，非自聚合估算；kind=stat 为涨跌家数等统计类板块。
    返回 {items:[{code,name,last,change_pct,amount,kind}], source, ts}。
    """
    kind = (kind or "industry").lower()
    if kind not in ("industry", "concept", "stat"):
        return err(400, f"kind 非法：{kind}（可选 industry/concept/stat）")
    # R5：sort_by 白名单校验（源层 get_boards 仅支持 pct/amount 两种排序键），
    # 非法值不再静默回落 pct，明确 400 并给出可选值。
    sort_by = (sort_by or "pct").lower()
    if sort_by not in ("pct", "amount"):
        return err(400, f"sort_by 非法：{sort_by}（可选 pct=涨跌幅 / amount=成交额）")
    ck = f"boards:{kind}:{sort_by}:{limit}:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    try:
        items, src_name = await get_hub().get_boards(kind, sort_by, limit, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"板块榜获取失败：{exc}")
    if not items:
        return err(503, "板块榜获取失败：TDX 行情源暂不可用，请检查网络或连接券商。")
    out = {"items": items, "kind": kind, "count": len(items), "source": src_name,
           "ts": datetime.now().isoformat(timespec="seconds")}
    if ttl > 0:
        _ttl_set(ck, out)
    return ok(out)


@router.get("/market/board/constituents")
async def market_board_constituents(code: str, limit: int = 100, page: int = 0,
                                    source: str = "auto", ttl: int = 15):
    """板块成分股（真实板块成分，非全市场过滤）。

    page/limit 透传 f10 分页；单页上限 200。返回
    {code,total,items,page,page_size,has_more,page_count,source}，
    大板块（电子 548 只）可翻页取全量，不再只显示前 50 只（P0-3）。
    """
    if not code:
        return err(400, "缺少板块代码 code")
    limit = max(1, min(int(limit or 100), 200))
    page = max(0, int(page or 0))
    ck = f"cons:{code}:{page}:{limit}:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    try:
        res, src_name = await get_hub().get_board_constituents(code, limit, page, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"成分股获取失败：{exc}")
    if not res or not res.get("items"):
        return err(503, f"成分股获取失败：{code} 暂无成分数据（统计类板块无成分或网络异常）。")
    out = {**res, "source": src_name}
    if ttl > 0:
        _ttl_set(ck, out)
    return ok(out)


@router.get("/market/board/lookup")
async def market_board_lookup(name: str, limit: int = 8, source: str = "auto",
                              ttl: int = 600):
    """板块名称 → 代码匹配（P1-8 深链稳化）。

    个股页只有行业/概念「名称」，前端靠 name.includes 模糊匹配板块榜会命中错项
    （「半导体」vs「半导体概念」），榜单未加载时更是静默失败（点击无反应）。
    此处做 完全一致 → 前缀 → 包含 三级匹配，前端据 code 精确选中。
    返回 {name, matches:[{code,name,kind,metric,unit}], source}。
    """
    name = (name or "").strip()
    if not name:
        return err(400, "缺少板块名称 name")
    limit = max(1, min(int(limit or 8), 20))
    ck = f"boardlookup:{name}:{limit}:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    try:
        matches, src_name = await get_hub().search_boards(name, limit, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"板块匹配失败：{exc}")
    out = {"name": name, "matches": matches or [], "source": src_name}
    if ttl > 0:
        _ttl_set(ck, out)
    return ok(out)


@router.get("/market/board/kline")
async def market_board_kline(code: str, period: str = "1d", count: int = 60,
                             source: str = "auto", ttl: int = 60):
    """板块 / 指数 K 线（内部按 kind=index 取，避免 ProtocolError）。

    R4（P2-3 漏网）：加 TTL 缓存（key 含 code/period/count/source），
    与 constituents 同模式 —— 多窗口/翻看同板块时 60s 内不重复打源。
    """
    try:
        # 未知周期 UnknownPeriodError / 数据源不支持 UnsupportedPeriodError，
        # 二者均为 ValueError 子类；绝不静默降级为日线。
        from app.datasource.periods import to_eltdx_period
        to_eltdx_period(period)
    except ValueError as exc:
        return err(400, str(exc))
    ck = f"bkline:{code}:{period}:{count}:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    try:
        bars, src_name = await get_hub().get_board_kline(code, period, count, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"板块 K 线获取失败：{exc}")
    if not bars:
        return err(503, f"板块 K 线获取失败：{code} 暂无数据。")
    out = {"code": code, "period": period, "bars": bars, "source": src_name}
    if ttl > 0:
        _ttl_set(ck, out)
    return ok(out)


# ETF 快照并发上限：无并发控制时 800 只逐只打 TDX 快照约 95s（实测 60 只 7.15s），
# 远超前端 15s 超时，页面必然失败（P0-1）。限流到 8 路后仍可控，且首屏已不依赖全量快照。
_ETF_QUOTE_SEM = asyncio.Semaphore(8)
_ETF_QUOTE_CAP = 150      # with_quote 单批最多补齐的只数
_ETF_LIST_TTL = 300       # ETF 清单 5 分钟缓存（清单基本不日内变化）


@router.get("/market/etfs")
async def market_etfs(limit: int = 0, with_quote: bool = False,
                      quote_limit: int = _ETF_QUOTE_CAP, source: str = "auto",
                      ttl: int = _ETF_LIST_TTL):
    """ETF 全市场清单（代码段 51/56/58/15/16）。

    P0-1 修复：清单本身加 TTL 缓存；with_quote 快照加并发 Semaphore + 只数上限，
    避免 800 只逐只打源（约 95s）超时导致页面根本加载不出来。
    P0-2 修复：limit<=0 返回全量，不再按 code 升序截断导致 56/58 段整段丢失。
    实时价建议由前端 QuoteHub 订阅可见行 + /market/quotes 批量补齐，而非全量快照。
    返回 {items, count, groups:{前缀:数量}, quote_capped, source, ts}。
    """
    ck = f"etfs:{limit}:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    try:
        items, src_name = await get_hub().get_etf_list(limit or 0, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"ETF 清单获取失败：{exc}")
    if not items:
        return err(503, "ETF 清单获取失败：TDX 行情源暂不可用。")
    groups = {}
    for it in items:
        g = str(it.get("code", ""))[:2]
        groups[g] = groups.get(g, 0) + 1
    out = {"items": items, "count": len(items), "groups": groups,
           "quote_capped": False, "source": src_name,
           "ts": datetime.now().isoformat(timespec="seconds")}

    if with_quote:
        codes = [it["code"] for it in items]
        capped = len(codes) > quote_limit
        codes = codes[:quote_limit]
        m = get_hub()

        async def _one(c):
            async with _ETF_QUOTE_SEM:
                try:
                    return c, await asyncio.wait_for(m.get_quote(c, source=source), timeout=5)
                except Exception:  # noqa: BLE001
                    return c, None

        res = await asyncio.gather(*[_one(c) for c in codes])
        qmap = {c: q for c, q in res if q}
        items = [{**it, "last": qmap[it["code"]].get("last"),
                  "change_pct": qmap[it["code"]].get("change_pct"),
                  "amount": qmap[it["code"]].get("amount")}
                 for it in items if it["code"] in qmap]
        out = {"items": items, "count": len(items), "groups": groups,
               "quote_capped": capped, "quote_limit": quote_limit,
               "source": src_name,
               "ts": datetime.now().isoformat(timespec="seconds")}
    if ttl > 0 and items:
        _ttl_set(ck, out)
    return ok(out)


@router.get("/market/moneyflow")
async def market_moneyflow(code: str, source: str = "auto"):
    """个股资金流（真实口径：快照内外盘 + 分钟级买卖力道 + 量比）。

    任一字段缺失返回 null，由前端显式显示「—」，禁止估算填充。
    返回 {code,inside,outside,net,strength:[{t,buy,sell}],volume_ratio,est,ts}。
    """
    if not code:
        return err(400, "缺少股票代码 code")
    try:
        res, src_name = await get_hub().get_moneyflow(code, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"资金流获取失败：{exc}")
    if not res:
        return err(503, f"资金流获取失败：{code} 暂无 TDX 资金流数据（非交易时段或代码不受支持）。")
    return ok({**res, "source": src_name})


@router.get("/market/capital")
async def market_capital(codes: str, source: str = "auto", ttl: int = 300):
    """批量流通股本 + 涨跌停价（换手率与涨跌停展示的真实口径来源）。

    codes: 逗号分隔（最多 50 只）。返回 {shares:{code:{...}}, limits:{code:{...}}}。
    """
    want = [c.strip() for c in (codes or "").split(",") if c.strip()][:50]
    if not want:
        return err(400, "缺少股票代码 codes")
    ck = f"capital:{','.join(want)}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    shares_task = asyncio.wait_for(
        get_hub().get_share_capital(want, source=source), timeout=10)
    limits_task = asyncio.wait_for(
        get_hub().get_price_limits(want, source=source), timeout=10)
    (shares, _), (limits, _) = await asyncio.gather(shares_task, limits_task)
    out = {"shares": shares or {}, "limits": limits or {}}
    if ttl > 0 and (out["shares"] or out["limits"]):
        _ttl_set(ck, out)
    return ok(out)


# ===================== G2 板块资金流（成分股加权聚合） =====================
_BOARD_MF_SEM = asyncio.Semaphore(10)   # 成分股资金流并发上限（避免连接风暴）


@router.get("/market/board/moneyflow")
async def market_board_moneyflow(code: str, source: str = "auto",
                                 top_n: int = 30, ttl: int = 30):
    """板块资金流：聚合成分股当日主力净流入（外盘-内盘），真实口径。

    成分股逐只取资金流（并发限流），汇总板块级 当日净流入 / 内外盘总量 /
    贡献度排名（top_n）。分钟级(5/10/60m)拆分 eltdx 仅提供日累计快照，
    故此处只给「当日」口径并在返回中标明 granularity='day'，不伪造分钟序列。
    返回 {code,count,total_net,total_inside,total_outside,
          contributors:[{code,name,net,pct}], granularity, source, ts}。
    """
    if not code:
        return err(400, "缺少板块代码 code")
    # 域校验（快失败）：板块资金流仅对 TDX 板块指数有意义（881 行业 / 880 概念·统计）。
    # 无此前置校验时，未知码会触发源层的全市场回落聚合（800 只逐只资金流 >90s），
    # 既拖垮调用方也浪费打源额度。非法码直接 400。
    if (code or "").upper()[:3] not in ("881", "880"):
        return err(400, f"非板块代码（须 881xxx/880xxx）：{code}")
    top_n = max(1, min(int(top_n or 30), 100))
    ck = f"boardmf:{code}:{top_n}:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    # 1) 翻页取全量成分股
    cons = []
    page = 0
    while True:
        try:
            res, _ = await get_hub().get_board_constituents(code, 200, page, source=source)
        except Exception as exc:  # noqa: BLE001
            return err(503, f"板块成分股获取失败：{exc}")
        if not res or not res.get("items"):
            break
        cons.extend(res["items"])
        if not res.get("has_more") or len(cons) >= 800:
            break
        page += 1
    if not cons:
        return err(503, f"板块资金流获取失败：{code} 暂无成分数据。")
    codes = [c["code"] for c in cons if c.get("code")]
    names = {c["code"]: c.get("name", "") for c in cons if c.get("code")}
    m = get_hub()

    async def _one(c):
        async with _BOARD_MF_SEM:
            try:
                r, _ = await asyncio.wait_for(m.get_moneyflow(c, source=source), timeout=5)
                return c, r
            except Exception:  # noqa: BLE001
                return c, None

    rows = await asyncio.gather(*[_one(c) for c in codes])
    recs = []
    for c, r in rows:
        if not r:
            continue
        net = r.get("net")
        inside = r.get("inside")
        outside = r.get("outside")
        if net is None and (inside is None or outside is None):
            continue
        if net is None:
            net = (outside - inside) if (inside is not None and outside is not None) else 0
        recs.append({"code": c, "name": names.get(c, ""), "net": net,
                     "inside": inside, "outside": outside})
    total_net = round(sum(x["net"] for x in recs), 2)
    total_inside = round(sum(x["inside"] or 0 for x in recs), 2)
    total_outside = round(sum(x["outside"] or 0 for x in recs), 2)
    recs.sort(key=lambda x: abs(x["net"] or 0), reverse=True)
    contributors = []
    denom = abs(total_net) or 1
    for x in recs[:top_n]:
        contributors.append({"code": x["code"], "name": x["name"],
                             "net": round(x["net"], 2),
                             "pct": round((x["net"] or 0) / denom * 100, 2) if denom else 0})
    out = {"code": code, "count": len(recs),
           "total_net": total_net, "total_inside": total_inside, "total_outside": total_outside,
           "contributors": contributors, "granularity": "day", "source": source,
           "ts": datetime.now().isoformat(timespec="seconds")}
    if ttl > 0:
        _ttl_set(ck, out)
    return ok(out)


# ===================== F4 板块轮动矩阵 =====================
_ROT_SEM = asyncio.Semaphore(6)


@router.get("/market/rotation")
async def market_rotation(days: int = 5, kind: str = "industry",
                          top_n: int = 40, source: str = "auto"):
    """板块轮动：取板块榜 topN（按 |涨跌幅|），各取日K 计算每日%chg，返回矩阵供热力图。

    days: 回看交易日数（3~20）；top_n: 参与板块数（10~60）。
    返回 {days, kind, boards:[{code,name,total_pct,daily:[{date,pct}],cum_pct}], source, ts}。
    """
    days = max(3, min(int(days or 5), 20))
    top_n = max(10, min(int(top_n or 40), 60))
    kind = (kind or "industry").lower()
    if kind not in ("industry", "concept"):
        return err(400, f"kind 非法：{kind}（轮动仅支持 industry/concept）")
    try:
        boards, _ = await get_hub().get_boards(kind, "pct", 300, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"板块榜获取失败：{exc}")
    if not boards:
        return err(503, "板块榜获取失败：TDX 行情源暂不可用。")
    boards.sort(key=lambda b: abs(b.get("change_pct") or 0), reverse=True)
    pick = boards[:top_n]

    async def _kline(b):
        async with _ROT_SEM:
            try:
                bars, _ = await asyncio.wait_for(
                    get_hub().get_board_kline(b["code"], "1d", days + 1, source=source), timeout=8)
                return b, bars
            except Exception:  # noqa: BLE001
                return b, None

    res = await asyncio.gather(*[_kline(b) for b in pick])
    out_boards = []
    for b, bars in res:
        if not bars or len(bars) < 2:
            continue
        closes = [float(x.get("close")) for x in bars if x.get("close")]
        dates = [x.get("time") for x in bars if x.get("close")]
        daily = []
        for i in range(1, len(closes)):
            pct = round((closes[i] / closes[i - 1] - 1) * 100, 2) if closes[i - 1] else 0
            daily.append({"date": dates[i], "pct": pct})
        cum = round((closes[-1] / closes[0] - 1) * 100, 2) if closes[0] else 0
        out_boards.append({"code": b["code"], "name": b.get("name", ""),
                           "total_pct": b.get("change_pct"),
                           "daily": daily, "cum_pct": cum})
    out = {"days": days, "kind": kind, "boards": out_boards, "source": source,
           "ts": datetime.now().isoformat(timespec="seconds")}
    return ok(out)


# ===================== E3 市场概览（广度/指数/宽度趋势） =====================
_BREADTH_TREND_CODE = "880005.SH"   # 涨跌家数（统计类板块，真实家数）


@router.get("/market/overview")
async def market_overview(source: str = "auto", ttl: int = 10):
    """市场概览（E3）：统计类板块真实家数（涨跌/停板/各市场）+ 主要指数快照 + 宽度趋势。

    两市成交额（C4）：上证+深证指数快照 amount 求和（真实口径，零额外打源）；
    任一市场缺失则 two_city_turnover=null 由前端显式标注「—」，不伪造。
    返回 {breadth:[{code,name,count,metric,unit}], indices:[{code,name,last,change_pct,amount}],
          breadth_trend:{code,name,series:[{date,value}]}, two_city_turnover, source, ts}。
    """
    ck = f"overview:{source}"
    if ttl > 0:
        hit = _ttl_get(ck, ttl)
        if hit is not None:
            return ok(hit)
    try:
        stat, _ = await get_hub().get_boards("stat", "pct", 60, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"市场概览获取失败：{exc}")
    breadth = [{"code": b["code"], "name": b.get("name", ""),
                "count": b.get("last"), "metric": b.get("metric"), "unit": b.get("unit")}
               for b in (stat or [])]
    # 主要指数快照（B4：跟随 runtime_config 配置的指数清单）
    async def _iq(c):
        try:
            return c, await asyncio.wait_for(get_hub().get_quote(c, source=source), timeout=6)
        except Exception:  # noqa: BLE001
            return c, None
    ires = await asyncio.gather(*[_iq(c) for c in _configured_indices()])
    indices = []
    for c, q in ires:
        if q:
            indices.append({"code": c, "name": q.get("name", ""), "last": q.get("last"),
                            "change_pct": q.get("change_pct"), "amount": q.get("amount")})
    # 宽度趋势：涨跌家数日K（真实家数时间序列）
    trend = None
    try:
        bars, _ = await asyncio.wait_for(
            get_hub().get_board_kline(_BREADTH_TREND_CODE, "1d", 30, source=source), timeout=8)
        if bars:
            trend = {"code": _BREADTH_TREND_CODE, "name": "涨跌家数",
                     "series": [{"date": b.get("time"), "value": b.get("close")} for b in bars]}
    except Exception:  # noqa: BLE001
        trend = None
    # C4 两市成交额：真实聚合 —— TDX 指数快照的 amount 即该市场成交总额，
    # 上证(000001.SH) + 深证(399001.SZ) 求和即为两市成交额（零额外打源，非估算口径）。
    # 任一市场缺 amount 则整体置 None（显「—」），绝不编数。
    amt_sh = next((q.get("amount") for c, q in ires if c == "000001.SH" and q), None)
    amt_sz = next((q.get("amount") for c, q in ires if c == "399001.SZ" and q), None)
    two_city = None
    if amt_sh is not None and amt_sz is not None:
        two_city = float(amt_sh) + float(amt_sz)
    out = {"breadth": breadth, "indices": indices,
           "breadth_trend": trend, "two_city_turnover": two_city,
           "two_city_note": ("上证+深证指数快照成交额求和（真实口径）" if two_city is not None
                             else "指数快照缺成交额，无法聚合两市成交额"),
           "source": source, "ts": datetime.now().isoformat(timespec="seconds")}
    if ttl > 0:
        _ttl_set(ck, out)
    return ok(out)


# ===================== G3 资金流落库 / 回放 / 自动采集 =====================
class _MoneyflowSnapshotReq(BaseModel):
    codes: list = []
    board: str = ""


async def _snapshot_codes(codes: list) -> int:
    """逐只取资金流并落库 moneyflow_cache，返回插入条数。"""
    if not codes:
        return 0
    m = get_hub()

    async def _one(c):
        async with _BOARD_MF_SEM:
            try:
                r, _ = await asyncio.wait_for(m.get_moneyflow(c, source="auto"), timeout=5)
                return c, r
            except Exception:  # noqa: BLE001
                return c, None

    got = await asyncio.gather(*[_one(c) for c in codes])
    db = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    for c, r in got:
        if not r:
            continue
        net = r.get("net")
        if net is None:
            net = (r.get("outside") - r.get("inside")) if (
                r.get("inside") is not None and r.get("outside") is not None) else None
        db.insert("moneyflow_cache", {
            "code": c, "name": r.get("code") or c, "ts": ts,
            "inside": r.get("inside"), "outside": r.get("outside"),
            "net": net, "volume_ratio": r.get("volume_ratio"),
            "source": r.get("source") or "auto"})
        inserted += 1
    return inserted


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
    inserted = await _snapshot_codes(codes)
    return ok({"inserted": inserted, "codes": codes,
               "ts": datetime.now().isoformat(timespec="seconds")})


@router.get("/market/moneyflow/replay")
async def market_moneyflow_replay(code: str, date: str = "", limit: int = 500):
    """资金流回放：取 code 的历史快照序列（按 ts 升序）。date=YYYY-MM-DD 可选过滤某日。"""
    if not code:
        return err(400, "缺少 code")
    db = get_db()
    limit = max(1, min(int(limit or 500), 5000))
    if date:
        rows = db.query(
            "SELECT code,name,ts,inside,outside,net,volume_ratio,source "
            "FROM moneyflow_cache WHERE code=? AND ts LIKE ? ORDER BY ts ASC LIMIT ?",
            (code, f"{date}%", limit))
    else:
        rows = db.query(
            "SELECT code,name,ts,inside,outside,net,volume_ratio,source "
            "FROM moneyflow_cache WHERE code=? ORDER BY ts ASC LIMIT ?",
            (code, limit))
    return ok({"code": code, "rows": rows, "count": len(rows)})


# 资金流自动采集观测池（代表性大盘蓝筹；get_moneyflow 失败者自动跳过）。
_MF_WATCHLIST = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "600036.SH",
                 "000858.SZ", "002594.SZ", "600900.SH", "601012.SH", "600276.SH",
                 "000333.SZ", "600030.SH", "601888.SH", "603259.SH", "300059.SZ"]


async def _moneyflow_collector_loop():
    """交易时段每 5 分钟采集观测池资金流快照（G3 自动归档）。

    C2：观测池经 runtime_config `market.moneyflow.watchlist` 可配置
    （逗号分隔 QMT 代码），留空回退内置 _MF_WATCHLIST，每轮热读取。
    """
    while True:
        try:
            now = datetime.now()
            if now.weekday() < 5:   # 工作日
                hm = now.hour * 60 + now.minute
                in_am = 570 <= hm <= 690      # 9:30-11:30
                in_pm = 780 <= hm <= 900      # 13:00-15:00
                if in_am or in_pm:
                    watch = _MF_WATCHLIST
                    try:
                        rc = getattr(state, "runtime_config", None)
                        raw = (rc.get("market.moneyflow.watchlist") or "") if rc else ""
                        conf = [c.strip() for c in str(raw).replace("，", ",").split(",") if c.strip()]
                        if conf:
                            watch = conf
                    except Exception:  # noqa: BLE001
                        pass
                    n = await _snapshot_codes(watch)
                    if n:
                        log.info("资金流自动采集 %d 条", n)
        except Exception as exc:  # noqa: BLE001
            log.warning("资金流自动采集异常：%s", exc)
        await asyncio.sleep(300)


def start_moneyflow_collector():
    """在 lifespan 启动资金流自动采集后台任务（需在事件循环内调用）。"""
    try:
        asyncio.create_task(_moneyflow_collector_loop())
        log.info("资金流自动采集已启动（交易时段每 5 分钟）")
    except Exception as exc:  # noqa: BLE001
        log.warning("资金流自动采集启动失败：%s", exc)


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
    kc = state.kline_cache
    dest = str(body.get("dest_dir") or "").strip()
    if not dest:
        return err(400, "请提供 dest_dir（导出到本地指定目录）")
    fmt = (body.get("format") or "csv").lower()
    if fmt not in ("csv", "json", "feather"):
        return err(400, "format 仅支持 csv / json / feather")
    count = int(body.get("count") or 0)
    refresh = bool(body.get("refresh", False))
    want_codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    want_period = str(body.get("period") or "").strip() or None

    series = kc.all_series()
    if want_codes or want_period:
        def _match(s):
            if want_codes and s["code"] not in want_codes:
                return False
            if want_period and s["period"] != want_period:
                return False
            return True
        series = [s for s in series if _match(s)]
    if not series:
        return ok({"dest_dir": dest, "format": fmt, "refresh": refresh,
                   "exported": 0, "files": []})

    if refresh:
        # 回源刷新到缓存后再导出（需要 broker 连接；单只失败不中断整体）
        from tools import fetch_kline_cached
        for s in series:
            try:
                await fetch_kline_cached(s["code"], s["period"], max(count, 250) or 250,
                                         broker_id=body.get("conn_id") or None)
            except BrokerError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("kline export refresh skip %s: %s", s["code"], exc)

    results = []
    for s in series:
        r = await asyncio.to_thread(kc.export_to, s["code"], s["period"], dest, fmt, count)
        results.append(r)
    total_rows = sum(x["rows"] for x in results)
    state.db.audit("admin", "kline.export", dest,
                   {"format": fmt, "refresh": refresh, "dest_dir": dest},
                   f"files={len(results)} rows={total_rows}")
    return ok({"dest_dir": dest, "format": fmt, "refresh": refresh,
               "exported": len(results), "rows": total_rows, "files": results})


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
    参数（body JSON）：
    - dest_dir: 必填，同步导出目录（不存在自动创建）
    - codes: 股票代码列表；省略时用 sector 板块成分（默认「沪深A股」= 全部 A 股）
    - sector: codes 省略时使用的板块，默认 沪深A股
    - periods: 要同步的周期，默认 ["1d","1w"]
    - count: 每序列历史根数，默认 250
    - format: csv | json | feather，默认 csv
    - limit: 最大处理的股票数（0=全部），便于控制全市场大批量同步
    - conn_id: 指定 broker 连接；省略用活跃连接

    单只失败不中断整体（errors 列出）。真实行情，缺数据不伪造。
    """
    if state.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    kc = state.kline_cache
    dest = str(body.get("dest_dir") or "").strip()
    if not dest:
        return err(400, "请提供 dest_dir（同步导出到本地指定目录）")
    fmt = (body.get("format") or "csv").lower()
    if fmt not in ("csv", "json", "feather"):
        return err(400, "format 仅支持 csv / json / feather")
    periods = [str(p).strip() for p in (body.get("periods") or ["1d", "1w"]) if str(p).strip()]
    if not periods:
        periods = ["1d"]
    count = int(body.get("count") or 250)
    limit = int(body.get("limit") or 0)

    codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    if not codes:
        b = _need(body.get("conn_id") or None)
        if b is None:
            return err(503, "未连接任何券商客户端：省略 codes 需用板块成分，请先连接券商。")
        sector = str(body.get("sector") or "沪深A股")
        codes = await _call(b, b.gateway.get_sector_stocks, sector) or []
        if not codes:
            return err(404, f"板块成交为空：{sector}")
    if limit > 0:
        codes = codes[:limit]

    from tools import fetch_kline_cached
    from gateway.kline_cache import resample_weekly
    files, errors = [], []
    for idx, code in enumerate(codes, 1):
        _need_daily = any(p in ("1w", "week") for p in periods)
        daily_bars: list[dict] = []
        try:
            # 先取日线（若待同步含周线，作为原生周线不可用时的兜底数据源）
            if _need_daily or "1d" in periods:
                dres = await fetch_kline_cached(code, "1d", max(count, 10) or 250,
                                                broker_id=body.get("conn_id") or None, force=True)
                daily_bars = dres.get("bars") or []
            for per in periods:
                res = await fetch_kline_cached(code, per, max(count, 10) or 250,
                                               broker_id=body.get("conn_id") or None, force=True)
                bars = res.get("bars") or []
                # 原生周线接口不可用/返回空时，用同源日线聚合出周线兜底（不伪造行情）
                if not bars and per in ("1w", "week") and daily_bars:
                    weekly = resample_weekly(daily_bars)
                    if weekly:
                        await kc.aput(code, per, weekly)
                        bars = weekly
            for per in periods:
                r = await asyncio.to_thread(kc.export_to, code, per, dest, fmt, count)
                if r.get("file"):
                    files.append(r)
        except Exception as exc:  # noqa: BLE001
            errors.append({"code": code, "error": str(exc)[:200]})
            log.warning("kline sync skip %s: %s", code, exc)
    total_rows = sum(x["rows"] for x in files)
    state.db.audit("admin", "kline.sync", dest,
                   {"format": fmt, "periods": periods, "count": count, "codes": len(codes)},
                   f"files={len(files)} rows={total_rows} errors={len(errors)}")
    return ok({"dest_dir": dest, "format": fmt, "periods": periods,
               "codes_total": len(codes), "files": files,
               "files_count": len(files), "rows": total_rows, "errors": errors})


# ---------------- 行情爬虫（真实 K 线落库） ----------------

@router.post("/market/crawl")
async def crawl_market(body: dict):
    b = _need(body.get("conn_id") or None)
    if b is None:
        return err(503, "未连接任何券商客户端。")
    codes = body.get("codes", ["600519.SH"])
    days = int(body.get("days", 30))
    period = str(body.get("period") or "1d")
    adjust = str(body.get("adjust") or "")
    cache = getattr(state, "kline_cache", None)
    inserted = 0
    for code in codes:
        bars = await _call(b, b.gateway.get_kline, code, period, days)
        if isinstance(bars, dict) and bars.get("code"):
            return bars
        if cache is None:
            # 无缓存引擎时退化为老逻辑（写入 market_cache 兜底），避免空操作
            for bb in bars:
                try:
                    state.db.upsert("market_cache", {
                        "code": code, "dtype": "kline", "ts": bb.get("time", ""),
                        "payload_json": json.dumps(bb, ensure_ascii=False)})
                    inserted += 1
                except Exception:
                    pass
        else:
            # 统一经 KlineCache 落库（热/归档分离）：抓取结果直接进入图表/回测查询链路
            inserted += await cache.aput(code, period, bars, adjust)
    return ok({"crawled_codes": codes, "bars_inserted": inserted})


# ---------------- LLM 配置（加密存储） ----------------

@router.get("/market/l2")
async def market_l2(code: str, count: int = 100):
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_l2_transactions, code, count)


# ---------------- 策略模板库 ----------------

