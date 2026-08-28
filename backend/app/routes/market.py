from app.routes._common import ok, err, state, _need, _call, BrokerError

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import json
import logging
import os

from app.datasource.board import classify_board
from app.datasource.manager import get_hub, MarketDataUnavailable

log = logging.getLogger("qmt_work.market")


router = APIRouter()


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
        if source == "eltdx":
            return err(503, f"TDX 行情源不可用：{code}（请检查网络或连接券商）")
        if source == "broker":
            return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
        return err(503, "行情获取失败：券商未连接且 TDX 行情源不可用，请连接券商或检查网络。")
    return q


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


@router.get("/market/kline")
async def market_kline(code: str, period: str = "1d", count: int = 250,
                       conn_id: str = "", force: bool = False, source: str = "auto",
                       adj: str = ""):
    """历史 K 线（C1 本地缓存优先；source: auto=券商优先回退eltdx / broker / eltdx）。
    adj: ''=不复权 / qfq=前复权 / hfq=后复权。券商 get_kline 不支持复权，显式复权时
    走 TDX 复权源，避免静默返回原始价误导用户。"""
    from tools import fetch_kline_cached
    try:
        res = await fetch_kline_cached(code, period, count,
                                       broker_id=conn_id or None, force=force,
                                       source=source,
                                       adjust=adj or None)
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"code": code, "period": period, "count": len(res.get("bars") or []),
               "source": res.get("source"), "cached_at": res.get("cached_at"),
               "note": res.get("note"), "adjust": adj or "",
               "bars": res.get("bars") or []})

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

