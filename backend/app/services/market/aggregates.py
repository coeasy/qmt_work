"""多维行情编排：指数/板块/ETF/资金流/轮动/市场概览（自 routes/market.py 原样下沉）。

约定：函数返回 data 负载（dict，不含信封）；可预期失败抛 ServiceError(code, message)，
由路由层 catch 后转 err() 信封。TTL 缓存与并发信号量参数与重构前一致。
"""
import asyncio
from datetime import datetime

from app.services.market.common import (
    BOARD_MF_SEM,
    ETF_QUOTE_CAP,
    ETF_QUOTE_SEM,
    INDICES_SEM,
    ROTATION_SEM,
    TTL,
    ServiceError,
    configured_indices,
)
from core.state import state
from datasource.registry import get_hub


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cached(key: str, ttl: int):
    """ttl>0 时查缓存，命中返回负载，未命中返回 None。"""
    if ttl > 0:
        return TTL.get(key, ttl)
    return None


def _store(key: str, ttl: int, out) -> None:
    if ttl > 0:
        TTL.set(key, out)


# ===================== 指数聚合快照 =====================

async def indices_snapshot(codes: str = "", source: str = "auto", ttl: int = 3,
                           spark: bool = False, spark_days: int = 20) -> dict:
    """主要指数聚合快照（顶部指数条数据源）。

    并发拉取，单只失败返回 null 并计入 errors，不因一只失败拖垮整条。
    codes: 逗号分隔，默认 DEFAULT_INDICES。ttl: 秒级缓存（0=不缓存）。
    spark=true 时附带近 spark_days 日收盘价序列（真实 K 线，kind=index）。
    """
    want = [c.strip() for c in (codes or "").split(",") if c.strip()] or configured_indices(state)
    ck = f"indices:{','.join(want)}:{source}:{int(bool(spark))}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    m = get_hub()

    async def _one(c):
        async with INDICES_SEM:
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
            async with INDICES_SEM:
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
           "ts": _now(), "source": source}
    if ttl > 0 and items and any(items):
        _store(ck, ttl, out)
    return out


# ===================== 板块榜 / 成分 / 名称匹配 / 板块K线 =====================

async def boards(kind: str = "industry", sort_by: str = "pct",
                 limit: int = 50, source: str = "auto", ttl: int = 10) -> dict:
    """板块榜单（行业 881xxx / 概念 880xxx / 统计类 stat）。

    走真实板块指数快照，非自聚合估算；kind=stat 为涨跌家数等统计类板块。
    """
    kind = (kind or "industry").lower()
    if kind not in ("industry", "concept", "stat"):
        raise ServiceError(400, f"kind 非法：{kind}（可选 industry/concept/stat）")
    # R5：sort_by 白名单校验（源层 get_boards 仅支持 pct/amount 两种排序键），
    # 非法值不再静默回落 pct，明确 400 并给出可选值。
    sort_by = (sort_by or "pct").lower()
    if sort_by not in ("pct", "amount"):
        raise ServiceError(400, f"sort_by 非法：{sort_by}（可选 pct=涨跌幅 / amount=成交额）")
    ck = f"boards:{kind}:{sort_by}:{limit}:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    try:
        items, src_name = await get_hub().get_boards(kind, sort_by, limit, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"板块榜获取失败：{exc}") from exc
    if not items:
        raise ServiceError(503, "板块榜获取失败：TDX 行情源暂不可用，请检查网络或连接券商。")
    out = {"items": items, "kind": kind, "count": len(items), "source": src_name, "ts": _now()}
    _store(ck, ttl, out)
    return out


async def board_constituents(code: str, limit: int = 100, page: int = 0,
                             source: str = "auto", ttl: int = 15) -> dict:
    """板块成分股（真实板块成分，非全市场过滤）。

    page/limit 透传 f10 分页；单页上限 200。大板块（电子 548 只）可翻页取全量（P0-3）。
    """
    if not code:
        raise ServiceError(400, "缺少板块代码 code")
    limit = max(1, min(int(limit or 100), 200))
    page = max(0, int(page or 0))
    ck = f"cons:{code}:{page}:{limit}:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    try:
        res, src_name = await get_hub().get_board_constituents(code, limit, page, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"成分股获取失败：{exc}") from exc
    if not res or not res.get("items"):
        raise ServiceError(503, f"成分股获取失败：{code} 暂无成分数据（统计类板块无成分或网络异常）。")
    out = {**res, "source": src_name}
    _store(ck, ttl, out)
    return out


async def board_lookup(name: str, limit: int = 8, source: str = "auto",
                       ttl: int = 600) -> dict:
    """板块名称 → 代码匹配（P1-8 深链稳化）。

    个股页只有行业/概念「名称」，前端靠 name.includes 模糊匹配板块榜会命中错项
    （「半导体」vs「半导体概念」），榜单未加载时更是静默失败（点击无反应）。
    此处做 完全一致 → 前缀 → 包含 三级匹配（由源层 search_boards 承担），
    前端据 code 精确选中。
    """
    name = (name or "").strip()
    if not name:
        raise ServiceError(400, "缺少板块名称 name")
    limit = max(1, min(int(limit or 8), 20))
    ck = f"boardlookup:{name}:{limit}:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    try:
        matches, src_name = await get_hub().search_boards(name, limit, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"板块匹配失败：{exc}") from exc
    out = {"name": name, "matches": matches or [], "source": src_name}
    _store(ck, ttl, out)
    return out


async def board_kline(code: str, period: str = "1d", count: int = 60,
                      source: str = "auto", ttl: int = 60) -> dict:
    """板块 / 指数 K 线（内部按 kind=index 取，避免 ProtocolError）。

    R4（P2-3 漏网）：加 TTL 缓存（key 含 code/period/count/source），
    与 constituents 同模式 —— 多窗口/翻看同板块时 60s 内不重复打源。
    """
    ck = f"bkline:{code}:{period}:{count}:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    try:
        bars, src_name = await get_hub().get_board_kline(code, period, count, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"板块 K 线获取失败：{exc}") from exc
    if not bars:
        raise ServiceError(503, f"板块 K 线获取失败：{code} 暂无数据。")
    out = {"code": code, "period": period, "bars": bars, "source": src_name}
    _store(ck, ttl, out)
    return out


# ===================== ETF 清单 =====================

async def etfs(limit: int = 0, with_quote: bool = False,
               quote_limit: int = ETF_QUOTE_CAP, source: str = "auto",
               ttl: int = 300) -> dict:
    """ETF 全市场清单（代码段 51/56/58/15/16）。

    P0-1 修复：清单本身加 TTL 缓存；with_quote 快照加并发 Semaphore + 只数上限，
    避免 800 只逐只打源（约 95s）超时导致页面根本加载不出来。
    P0-2 修复：limit<=0 返回全量，不再按 code 升序截断导致 56/58 段整段丢失。
    实时价建议由前端 QuoteHub 订阅可见行 + /market/quotes 批量补齐，而非全量快照。
    """
    ck = f"etfs:{limit}:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    try:
        items, src_name = await get_hub().get_etf_list(limit or 0, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"ETF 清单获取失败：{exc}") from exc
    if not items:
        raise ServiceError(503, "ETF 清单获取失败：TDX 行情源暂不可用。")
    groups = {}
    for it in items:
        g = str(it.get("code", ""))[:2]
        groups[g] = groups.get(g, 0) + 1
    out = {"items": items, "count": len(items), "groups": groups,
           "quote_capped": False, "source": src_name, "ts": _now()}

    if with_quote:
        codes = [it["code"] for it in items]
        capped = len(codes) > quote_limit
        codes = codes[:quote_limit]
        m = get_hub()

        async def _one(c):
            async with ETF_QUOTE_SEM:
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
               "source": src_name, "ts": _now()}
    if ttl > 0 and out["items"]:
        _store(ck, ttl, out)
    return out


# ===================== 资金流 / 股本 =====================

async def moneyflow(code: str, source: str = "auto") -> dict:
    """个股资金流（真实口径：快照内外盘 + 分钟级买卖力道 + 量比）。

    任一字段缺失返回 null，由前端显式显示「—」，禁止估算填充。
    """
    if not code:
        raise ServiceError(400, "缺少股票代码 code")
    try:
        res, src_name = await get_hub().get_moneyflow(code, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"资金流获取失败：{exc}") from exc
    if not res:
        raise ServiceError(503, f"资金流获取失败：{code} 暂无 TDX 资金流数据（非交易时段或代码不受支持）。")
    return {**res, "source": src_name}


async def capital(codes: str, source: str = "auto", ttl: int = 300) -> dict:
    """批量流通股本 + 涨跌停价（换手率与涨跌停展示的真实口径来源）。

    codes: 逗号分隔（最多 50 只）。
    """
    want = [c.strip() for c in (codes or "").split(",") if c.strip()][:50]
    if not want:
        raise ServiceError(400, "缺少股票代码 codes")
    ck = f"capital:{','.join(want)}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    shares_task = asyncio.wait_for(
        get_hub().get_share_capital(want, source=source), timeout=10)
    limits_task = asyncio.wait_for(
        get_hub().get_price_limits(want, source=source), timeout=10)
    (shares, _), (limits, _) = await asyncio.gather(shares_task, limits_task)
    out = {"shares": shares or {}, "limits": limits or {}}
    if ttl > 0 and (out["shares"] or out["limits"]):
        _store(ck, ttl, out)
    return out


# ===================== G2 板块资金流（成分股加权聚合） =====================

async def board_moneyflow(code: str, source: str = "auto",
                          top_n: int = 30, ttl: int = 30) -> dict:
    """板块资金流：聚合成分股当日主力净流入（外盘-内盘），真实口径。

    成分股逐只取资金流（并发限流），汇总板块级 当日净流入 / 内外盘总量 /
    贡献度排名（top_n）。分钟级(5/10/60m)拆分 eltdx 仅提供日累计快照，
    故此处只给「当日」口径并在返回中标明 granularity='day'，不伪造分钟序列。
    """
    if not code:
        raise ServiceError(400, "缺少板块代码 code")
    # 域校验（快失败）：板块资金流仅对 TDX 板块指数有意义（881 行业 / 880 概念·统计）。
    # 无此前置校验时，未知码会触发源层的全市场回落聚合（800 只逐只资金流 >90s），
    # 既拖垮调用方也浪费打源额度。非法码直接 400。
    if (code or "").upper()[:3] not in ("881", "880"):
        raise ServiceError(400, f"非板块代码（须 881xxx/880xxx）：{code}")
    top_n = max(1, min(int(top_n or 30), 100))
    ck = f"boardmf:{code}:{top_n}:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    # 1) 翻页取全量成分股
    cons = []
    page = 0
    while True:
        try:
            res, _ = await get_hub().get_board_constituents(code, 200, page, source=source)
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(503, f"板块成分股获取失败：{exc}") from exc
        if not res or not res.get("items"):
            break
        cons.extend(res["items"])
        if not res.get("has_more") or len(cons) >= 800:
            break
        page += 1
    if not cons:
        raise ServiceError(503, f"板块资金流获取失败：{code} 暂无成分数据。")
    codes = [c["code"] for c in cons if c.get("code")]
    names = {c["code"]: c.get("name", "") for c in cons if c.get("code")}
    m = get_hub()

    async def _one(c):
        async with BOARD_MF_SEM:
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
           "contributors": contributors, "granularity": "day", "source": source, "ts": _now()}
    _store(ck, ttl, out)
    return out


# ===================== F4 板块轮动矩阵 =====================

async def rotation(days: int = 5, kind: str = "industry",
                   top_n: int = 40, source: str = "auto") -> dict:
    """板块轮动：取板块榜 topN（按 |涨跌幅|），各取日K 计算每日%chg，返回矩阵供热力图。

    days: 回看交易日数（3~20）；top_n: 参与板块数（10~60）。
    """
    days = max(3, min(int(days or 5), 20))
    top_n = max(10, min(int(top_n or 40), 60))
    kind = (kind or "industry").lower()
    if kind not in ("industry", "concept"):
        raise ServiceError(400, f"kind 非法：{kind}（轮动仅支持 industry/concept）")
    try:
        boards_rows, _ = await get_hub().get_boards(kind, "pct", 300, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"板块榜获取失败：{exc}") from exc
    if not boards_rows:
        raise ServiceError(503, "板块榜获取失败：TDX 行情源暂不可用。")
    boards_rows.sort(key=lambda b: abs(b.get("change_pct") or 0), reverse=True)
    pick = boards_rows[:top_n]

    async def _kline(b):
        async with ROTATION_SEM:
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
    return {"days": days, "kind": kind, "boards": out_boards, "source": source, "ts": _now()}


# ===================== E3 市场概览（广度/指数/宽度趋势） =====================

_BREADTH_TREND_CODE = "880005.SH"   # 涨跌家数（统计类板块，真实家数）


OVERVIEW_BUDGET_SECONDS = 10


def _stat_named(rows: list, name: str):
    """按统计板块名称取真实值；找不到返回 None，绝不估算补 0。"""
    row = next((b for b in (rows or []) if b.get("name") == name), None)
    return row.get("last") if row else None


async def overview(source: str = "auto", ttl: int = 10) -> dict:
    """市场概览（E3）：统计类板块真实家数（涨跌/停板/各市场）+ 主要指数快照 + 宽度趋势。

    两市成交额（C4）：上证+深证指数快照 amount 求和（真实口径，零额外打源）；
    任一市场缺失则 two_city_turnover=null 由前端显式标注「—」，不伪造。
    breadth_summary 显式声明 TDX 统计口径：全市场只提供「涨跌差 / 停板家数 / 均价」，
    不把它拆成上涨家数 / 下跌家数 / 涨停家数，避免首屏伪造 0。
    """
    ck = f"overview:{source}"
    hit = _cached(ck, ttl)
    if hit is not None:
        return hit
    try:
        stat, _ = await asyncio.wait_for(
            get_hub().get_boards("stat", "pct", 60, source=source),
            timeout=OVERVIEW_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ServiceError(503, "市场概览获取超时：TDX 行情源响应慢。") from exc
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(503, f"市场概览获取失败：{exc}") from exc
    breadth = [{"code": b["code"], "name": b.get("name", ""),
                "count": b.get("last"), "metric": b.get("metric"), "unit": b.get("unit")}
               for b in (stat or [])]
    # 主要指数快照（B4：跟随 runtime_config 配置的指数清单）
    async def _iq(c):
        try:
            return c, await asyncio.wait_for(get_hub().get_quote(c, source=source), timeout=6)
        except Exception:  # noqa: BLE001
            return c, None
    ires = await asyncio.gather(*[_iq(c) for c in configured_indices(state)])
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
    breadth_summary = {
        "breadth_net": _stat_named(stat, "涨跌家数"),
        "breadth_net_prev": next((b.get("lastClose") for b in (stat or []) if b.get("name") == "涨跌家数"), None),
        "stopped_count": _stat_named(stat, "停板家数"),
        "avg_price": _stat_named(stat, "成交均价"),
        "note": "涨跌家数=上涨家数-下跌家数（TDX 统计口径）；停板家数为涨停/跌停合并家数。",
    }
    out = {"breadth": breadth, "breadth_summary": breadth_summary,
           "indices": indices,
           "breadth_trend": trend, "two_city_turnover": two_city,
           "two_city_note": ("上证+深证指数快照成交额求和（真实口径）" if two_city is not None
                             else "指数快照缺成交额，无法聚合两市成交额"),
           "source": source, "ts": _now()}
    _store(ck, ttl, out)
    return out
