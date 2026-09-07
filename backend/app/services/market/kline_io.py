"""K 线落库与导出/同步编排（自 routes/market.py 原样下沉）。

涵盖：资金流快照落库/自动采集、K 线导出(CSV/JSON/Feather)、全市场 K 线同步、爬虫落库。
"""
import asyncio
import json
import logging
from datetime import datetime

from xtquant_client.base import BrokerError

from app.datasource.registry import get_hub
from app.db import get_db
from app.services.market.common import BOARD_MF_SEM
from core.state import MSG_NO_BROKER, state

log = logging.getLogger("qmt_work.market")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ===================== G3 资金流落库 / 回放 / 自动采集 =====================

async def snapshot_codes(codes: list) -> int:
    """逐只取资金流并落库 moneyflow_cache，返回插入条数。"""
    if not codes:
        return 0
    m = get_hub()

    async def _one(c):
        async with BOARD_MF_SEM:
            try:
                r, _ = await asyncio.wait_for(m.get_moneyflow(c, source="auto"), timeout=5)
                return c, r
            except Exception:  # noqa: BLE001
                return c, None

    got = await asyncio.gather(*[_one(c) for c in codes])
    db = get_db()
    ts = _now()
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


# 资金流自动采集观测池（代表性大盘蓝筹；get_moneyflow 失败者自动跳过）。
MF_WATCHLIST = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "600036.SH",
                "000858.SZ", "002594.SZ", "600900.SH", "601012.SH", "600276.SH",
                "000333.SZ", "600030.SH", "601888.SH", "603259.SH", "300059.SZ"]


async def _moneyflow_collector_loop():
    """交易时段每 5 分钟采集观测池资金流快照（G3 自动归档）。

    C2：观测池经 runtime_config `market.moneyflow.watchlist` 可配置
    （逗号分隔 QMT 代码），留空回退内置 MF_WATCHLIST，每轮热读取。
    """
    while True:
        try:
            now = datetime.now()
            if now.weekday() < 5:   # 工作日
                hm = now.hour * 60 + now.minute
                in_am = 570 <= hm <= 690      # 9:30-11:30
                in_pm = 780 <= hm <= 900      # 13:00-15:00
                if in_am or in_pm:
                    watch = MF_WATCHLIST
                    try:
                        rc = getattr(state, "runtime_config", None)
                        raw = (rc.get("market.moneyflow.watchlist") or "") if rc else ""
                        conf = [c.strip() for c in str(raw).replace("，", ",").split(",") if c.strip()]
                        if conf:
                            watch = conf
                    except (AttributeError, TypeError) as exc:
                        # 配置缺失/类型异常：使用默认 watchlist，不阻断采集
                        log.debug("moneyflow.watchlist 配置读取失败，使用默认值：%s", exc)
                    n = await snapshot_codes(watch)
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


def moneyflow_replay(db, code: str, date: str = "", limit: int = 500) -> dict:
    """资金流回放：取 code 的历史快照序列（按 ts 升序）。date=YYYY-MM-DD 可选过滤某日。"""
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
    return {"code": code, "rows": rows, "count": len(rows)}


# ===================== K 线导出 / 同步 / 爬虫落库 =====================

async def kline_export(kc, body: dict) -> dict:
    """批量导出历史 K 线到本地指定目录（CSV / JSON / Feather）。

    参数（body JSON）：dest_dir(必填)/codes/period/count/format/refresh/conn_id。
    数据来自本地 K 线缓存（KlineCache），"快速"导出完全离线，无网络调用。
    """
    dest = str(body.get("dest_dir") or "").strip()
    if not dest:
        raise ValueError("请提供 dest_dir（导出到本地指定目录）")
    fmt = (body.get("format") or "csv").lower()
    if fmt not in ("csv", "json", "feather"):
        raise ValueError("format 仅支持 csv / json / feather")
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
        return {"dest_dir": dest, "format": fmt, "refresh": refresh,
                "exported": 0, "files": []}

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
    return {"dest_dir": dest, "format": fmt, "refresh": refresh,
            "exported": len(results), "rows": total_rows, "files": results}


async def kline_sync(kc, body: dict, get_sector_stocks) -> dict:
    """把一批股票的最新历史 K 线（含日线 1d、周线 1w）同步到本地指定目录。

    流程：确定股票集合 → 逐只回源券商拉取最新 K 线写入本地缓存 → 导出到 dest_dir。
    get_sector_stocks: (sector, conn_id) → codes 的回调（routes 层封装券商调用）。
    单只失败不中断整体（errors 列出）。真实行情，缺数据不伪造。
    """
    dest = str(body.get("dest_dir") or "").strip()
    if not dest:
        raise ValueError("请提供 dest_dir（同步导出到本地指定目录）")
    fmt = (body.get("format") or "csv").lower()
    if fmt not in ("csv", "json", "feather"):
        raise ValueError("format 仅支持 csv / json / feather")
    periods = [str(p).strip() for p in (body.get("periods") or ["1d", "1w"]) if str(p).strip()]
    if not periods:
        periods = ["1d"]
    count = int(body.get("count") or 250)
    limit = int(body.get("limit") or 0)

    codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    if not codes:
        codes = await get_sector_stocks(str(body.get("sector") or "沪深A股"),
                                        body.get("conn_id") or None)
        if not codes:
            raise LookupError(f"板块成交为空：{body.get('sector') or '沪深A股'}")
    if limit > 0:
        codes = codes[:limit]

    from gateway.kline_cache import resample_weekly
    from tools import fetch_kline_cached
    files, errors = [], []
    for code in codes:
        need_daily = any(p in ("1w", "week") for p in periods)
        daily_bars: list[dict] = []
        try:
            # 先取日线（若待同步含周线，作为原生周线不可用时的兜底数据源）
            if need_daily or "1d" in periods:
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
    return {"dest_dir": dest, "format": fmt, "periods": periods,
            "codes_total": len(codes), "files": files,
            "files_count": len(files), "rows": total_rows, "errors": errors}


async def crawl_market(body: dict) -> dict:
    """行情爬虫：真实 K 线落库（body: codes/days/period/adjust/conn_id）。"""
    b = state.broker_manager.bridge(body.get("conn_id") or None)
    if b is None:
        raise PermissionError(MSG_NO_BROKER)
    codes = body.get("codes", ["600519.SH"])
    days = int(body.get("days", 30))
    period = str(body.get("period") or "1d")
    adjust = str(body.get("adjust") or "")
    cache = getattr(state, "kline_cache", None)
    inserted = 0
    for code in codes:
        try:
            bars = await asyncio.wait_for(b.call(b.gateway.get_kline, code, period, days), 12)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"券商响应超时（>12s），请检查券商客户端是否已连接并登录") from exc
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
                except (AttributeError, OSError, ValueError) as exc:
                    # 单条 K 线写入失败：跳过该条，其他继续
                    log.debug("market_cache K 线写入失败（已跳过）：%s", exc)
        else:
            # 统一经 KlineCache 落库（热/归档分离）：抓取结果直接进入图表/回测查询链路
            inserted += await cache.aput(code, period, bars, adjust)
    return {"crawled_codes": codes, "bars_inserted": inserted}
