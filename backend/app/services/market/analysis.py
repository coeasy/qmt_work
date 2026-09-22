"""标的深度画像（/market/analysis 的业务编排，自 routes/market.py 原样下沉）。"""
import asyncio
import logging
from datetime import datetime

from datasource.instrument import classify_instrument, with_exchange_suffix
from core.clock import local_now, now_iso
from app.services.market.common import VALUATION_METRIC_MAP, VALUATION_METRIC_KEYS, metric_sources

log = logging.getLogger("qmt_work.market")


def perf_from_bars(bars):
    """近期表现：5/20/60 日涨跌幅 + 52 周高低点与现价分位 + as_of（数据截至日）。

    chg_Nd = close[-1]/close[-1-N] - 1（%）；52 周取近 250 根 high/low 极值；
    pct_in_52w = (last-low)/(high-low)×100。K 线不足 N 根的项为 None（不外推、不伪造）。
    """
    if not bars:
        return None
    valid = [b for b in bars if isinstance(b, dict) and b.get("close") is not None]
    closes = [b["close"] for b in valid]
    if len(closes) < 2:
        return None
    last = closes[-1]
    out = {"bars_used": len(closes)}
    # 数据截至日：最后一根有效收盘 K 线的日期（供前端标注新鲜度，绝不静默旧数据）
    out["as_of"] = str(valid[-1].get("time") or "")[:10] or None
    for tag, n in (("chg_5d", 5), ("chg_20d", 20), ("chg_60d", 60)):
        base = closes[-1 - n] if len(closes) > n else None
        out[tag] = round((last / base - 1) * 100, 2) if base else None
    win = [b for b in bars if isinstance(b, dict)][-250:]
    highs = [b["high"] for b in win if b.get("high") is not None]
    lows = [b["low"] for b in win if b.get("low") is not None]
    hi = max(highs) if highs else None
    lo = min(lows) if lows else None
    out["high_52w"] = hi
    out["low_52w"] = lo
    out["pct_in_52w"] = round((last - lo) / (hi - lo) * 100, 1) \
        if (hi is not None and lo is not None and hi > lo) else None
    return out


def perf_stale(as_of, max_days: int = 10) -> bool:
    """表现数据是否陈旧：最后一根 K 线距今超过 max_days 个自然日（覆盖长假）。

    解析不了日期（as_of 缺失/脏格式）时不武断判陈旧，由 availability 如实标注。
    """
    s = "".join(ch for ch in str(as_of or "")[:10] if ch.isdigit())
    if len(s) != 8:
        return False
    try:
        d = datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return False
    return (local_now().date() - d).days > max_days


async def build_analysis(m, code: str, conn_id: str = "", source: str = "auto",
                         broker_of=None) -> dict:
    """标的深度画像：单请求并发聚合 6 维（快照/画像/股本/表现/资金流/估值）。

    每维独立超时容错（8s），单维失败只置 availability=unavailable，不拖垮整体。
    估值维度依赖券商财务数据，无券商连接时 unavailable（前端显示「估值需券商连接」）。
    PE/PB 用现价 / 每股收益 / 每股净资产现算（EPS≤0 时 PE 为 None，不伪造负值）。

    m: DataSourceManager；broker_of: conn_id → bridge 的取连接函数（无连接返回 None）。
    返回 data 负载（不含信封）。
    """
    # 统一补交易所后缀：券商只认 600519.SH，eltdx 名称表亦以此为键。
    # 不规范化 → 券商静默返空 + 名称查不到，两个维度同时「无数据」。
    code = with_exchange_suffix((code or "").strip().upper())
    if not code:
        raise ValueError("缺少 code")

    async def _guard(coro, timeout: float = 8.0):
        try:
            return await asyncio.wait_for(coro, timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("analysis 维度获取失败 %s：%s", code, exc)
            return None

    async def _snapshot():
        return await m.get_quote(code, source=source, conn_id=conn_id or None)

    async def _profile():
        return await m.get_instrument_detail(code, source=source, conn_id=conn_id or None)

    async def _capital():
        caps, _ = await m.get_share_capital([code])
        return (caps or {}).get(code)

    async def _performance():
        from tools import fetch_kline_cached
        res = await fetch_kline_cached(code, "1d", 250, broker_id=conn_id or None)
        perf = perf_from_bars(res.get("bars") or [])
        # 券商本地 K 线可能陈旧（QMT 客户端未同步该标的，如 ETF 段停在多年前）：
        # 检测 as_of 距今 >10 个自然日 → 用 TDX 公共源补最新真实 K 线重算；
        # 补数失败则保留原表现并显式标 stale（前端展示「数据截至 as_of」，不静默旧数据）。
        if perf and perf_stale(perf.get("as_of")):
            try:
                bars, _ = await m.get_kline(code, "1d", 250, source="eltdx",
                                            conn_id=conn_id or None)
                fresh = perf_from_bars(bars or [])
                if fresh and not perf_stale(fresh.get("as_of")):
                    return fresh
            except Exception as exc:  # noqa: BLE001
                log.debug("analysis 表现维度 TDX 补数失败 %s：%s", code, exc)
            perf["stale"] = True
        return perf

    async def _moneyflow():
        mf, _ = await m.get_moneyflow(code)
        return mf

    async def _valuation():
        b = broker_of(conn_id or None) if broker_of else None
        if b is None:
            return None
        return await b.call(b.gateway.get_financial, code)

    async def _valuation_metrics():
        """行情快照派生的估值指标（PE / PB）—— 券商财务缺位时的**第二路**。

        见 `valuation` 组合处的长注释：本维度此前的「无数据」是**假空缺**。
        逐源尝试「声明了行情派生字段」的源（`common.metric_sources()`，不写死
        腾讯，换源自动跟随）；任何异常都吞掉 —— 兜底是尽力而为，绝不让整个
        接口失败（失败时保持 unavailable，如实说没有）。
        """
        for name in metric_sources():
            try:
                q = await m.get_quote(code, source=name, conn_id=conn_id or None)
            except Exception as exc:  # noqa: BLE001
                log.debug("analysis 估值行情兜底源 %s 失败 %s：%s", name, code, exc)
                continue
            if not isinstance(q, dict):
                continue
            got = {k: q.get(k) for k in VALUATION_METRIC_KEYS if q.get(k) is not None}
            if got:
                got["_source"] = name
                return got
        return None

    snap, prof, cap, perf, mf, fin = await asyncio.gather(
        _guard(_snapshot()), _guard(_profile()), _guard(_capital()),
        _guard(_performance()), _guard(_moneyflow()), _guard(_valuation()),
    )

    last = (snap or {}).get("last")
    name = (prof or {}).get("name") or (snap or {}).get("name") or ""
    cls = classify_instrument(code, name)
    availability = {
        tag: ("ok" if data else "unavailable")
        for tag, data in (("snapshot", snap), ("profile", prof), ("capital", cap),
                          ("performance", perf), ("moneyflow", mf), ("valuation", fin))
    }
    # 表现维度陈旧（券商 K 线未同步且 TDX 补数失败）：ok → stale，前端展示数据截至日
    if perf and perf.get("stale"):
        availability["performance"] = "stale"

    # ---- 估值：券商财务优先，缺口用**行情派生指标**补齐 ----
    #
    # ★★ 为什么必须有「行情兜底」这一路（2026-09-21 实测，用户报「基本面 6 维
    #   数据不少维度无数据」）：
    #   本维度此前**只认券商财务接口**（`gateway.get_financial`）。实测同时成立：
    #     ① 本机 QMT 只有旧接口 `get_financial_data`，且返回 **10 张 0×0 空表**
    #        （终端未下载财务数据 / 无财务数据权限）；
    #     ② 与此同时 `/market/stock-info` 能拿到 **腾讯** 的
    #        `pe_ttm=19.23` / `pb=6.23` / `total_mv=1.57e12`。
    #   ⇒ 同一份估值数据，一个端点有、另一个端点说「无数据」——
    #     用户在「基本面」里看到的「估值 无数据」是**假空缺**：界面在撒谎，
    #     而真实原因（终端没财务数据）与展示口径（其实有 PE/PB）都不对。
    #
    #   现在补第二路：券商给不了就问声明了这些字段的行情源要，并如实标注
    #   `metrics_source`，让「这个 PE 是哪来的」可追溯（券商现算 / 行情源直供）。
    valuation = None
    fin_eps = (fin or {}).get("EPS")
    fin_bps = (fin or {}).get("BPS")
    if fin and (fin_eps is not None or fin_bps is not None):
        valuation = {
            "report_time": fin.get("report_time") or None, "eps": fin_eps, "bps": fin_bps,
            "roe": fin.get("ROE"), "detail": fin.get("detail", ""),
            "pe": round(last / fin_eps, 2) if (last and fin_eps and fin_eps > 0) else None,
            "pb": round(last / fin_bps, 2) if (last and fin_bps and fin_bps > 0) else None,
            "metrics_source": "broker",
        }
    if valuation is None or not (valuation.get("pe") or valuation.get("pb")):
        metrics = await _guard(_valuation_metrics(), timeout=8.0)
        if metrics:
            src_name = metrics.pop("_source", None)
            if valuation is None:
                valuation = {"report_time": None, "eps": None, "bps": None, "roe": None,
                             "detail": (fin or {}).get("detail", ""), "pe": None, "pb": None}
            # ★ 按 **显式映射**填，不能按同名填：行情源给的是 `pe_ttm`，载荷用 `pe`
            #   （实测按同名填会漏掉 PE —— pb 同名碰巧对上了，pe 一直空着）。
            for src_key, dst_key in VALUATION_METRIC_MAP.items():
                if valuation.get(dst_key) is None and metrics.get(src_key) is not None:
                    valuation[dst_key] = metrics[src_key]
            valuation["metrics_source"] = src_name
    if valuation:
        availability["valuation"] = "ok" if (valuation.get("pe") or valuation.get("pb")) \
            else "unavailable"

    if cap:
        circ, total = cap.get("circulating_shares"), cap.get("total_shares")
        vol = (snap or {}).get("volume")
        cap = {**cap,
               "turnover_rate": round(vol / circ * 100, 2) if (vol and circ) else None,
               "total_mktcap": round(last * total, 2) if (last and total) else None,
               "float_mktcap": round(last * circ, 2) if (last and circ) else None}

    return {
        "code": code, "name": name, "type": cls["type"],
        "exchange": cls["exchange"], "board": cls["board"], "label": cls["label"],
        "ts": now_iso(),
        "snapshot": snap, "profile": prof, "capital": cap,
        "performance": perf, "moneyflow": mf, "valuation": valuation,
        "availability": availability,
    }
