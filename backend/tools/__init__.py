"""工具层：REST / MCP 两端复用的 Python 函数（§4.1）。

所有工具通过 `get_bridge(conn_id)` 取得当前/指定券商连接的线程桥，
统一经 `bridge.call(...)` 调用真实券商 SDK，绝不产生假数据。
K 线在无券商连接时回退到 eltdx(TDX 公共行情) 数据源。
"""
import logging

from app.datasource.manager import get_hub
from app.state import state
from xtquant_client.base import BrokerError, BrokerNotConnectedError

log = logging.getLogger("qmt_work.tools")


def get_bridge(conn_id: str | None = None):
    """返回指定/活跃券商连接 bridge；无连接时抛 BrokerNotConnectedError。"""
    b = state.broker_manager.bridge(conn_id)
    if b is None:
        raise BrokerNotConnectedError(
            "当前未连接任何券商客户端：请到「券商连接」页添加并连接券商（国金/华鑫/银河等 MiniQMT）。")
    return b


async def fetch_kline_cached(code: str, period: str = "1d", count: int = 250,
                             broker_id: str | None = None, force: bool = False,
                             source: str = "auto", adjust: str | None = None) -> dict:
    """C1：缓存优先取历史 K 线。

    返回 {"bars": [...], "source": "cache"|"broker"|"eltdx"|"cache_stale", "cached_at": ts}。
    缓存未初始化时直接回源。source=auto 时券商优先，无连接/异常回退 eltdx(TDX 公共行情)。
    adjust: qfq/hfq/''。券商 get_kline 不支持复权，显式复权时优先走 eltdx 才能真正返回
    复权价（券商原始 K 线静默返回复权价会误导用户）；券商侧仅保持缓存里既有复权标记。

    ⚠️ 两条关键不变量（曾经因此出现「图表空白但无任何报错」）：
    1. 代码规范化：券商（xtquant）**只认 `600519.SH` 形态**。传裸代码 `600519`
       会静默返回空列表——不抛异常、不报错，于是既不走 except 分支也不回退
       eltdx，最终 K 线 0 根。入口统一补交易所后缀（幂等）。
    2. 空结果 = 失败：券商返回空列表属于「成功但无数据」，同样必须回退 eltdx，
       否则用户看到空白图表而无任何错误提示。
    """
    from app.datasource.instrument import with_exchange_suffix
    code = with_exchange_suffix(code)

    async def _fetch_broker(c: str, p: str, n: int):
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_kline, c, p, n)

    async def _fetch_eltdx(c: str, p: str, n: int):
        bars, src = await get_hub().get_kline(
            c, p, n, source="eltdx", conn_id=broker_id, adjust=adjust)
        return bars, src

    # 显式复权：券商不支持，直接走 TDX 复权源（失败再回退券商原始价）
    if adjust in ("qfq", "hfq") and period.lower() in ("1d", "day", "1w", "week", "1mon", "mon", "month"):
        try:
            bars, src = await _fetch_eltdx(code, period, count)
            await _persist(code, period, bars, adjust)
            return {"bars": bars, "source": src, "cached_at": None}
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 复权K线失败，回退券商原始K线: %s", exc)

    cache = getattr(state, "kline_cache", None)

    if source == "eltdx":
        bars, src = await _fetch_eltdx(code, period, count)
        await _persist(code, period, bars, adjust)
        return {"bars": bars, "source": src, "cached_at": None}

    async def _fallback_eltdx(reason: str) -> dict | None:
        """券商无数据/异常时回退 eltdx(TDX 公共行情)。仅 auto 链启用。"""
        if source != "auto":
            return None
        log.warning("K线回退 eltdx：%s", reason)
        try:
            bars, src = await _fetch_eltdx(code, period, count)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 回退同样失败（%s）：%s", code, exc)
            return None
        if not bars:
            return None
        await _persist(code, period, bars, adjust)
        return {"bars": bars, "source": src, "cached_at": None}

    if cache is None:
        # 无缓存层：直接回源券商，auto 时异常回退 eltdx
        try:
            res = await _fetch_broker(code, period, count)
        except Exception as exc:  # noqa: BLE001
            fb = await _fallback_eltdx(f"券商异常 {exc}")
            if fb is not None:
                return fb
            raise
        # 空结果 = 失败：券商静默返回空列表，必须回退而不是返回 0 根
        if not res:
            fb = await _fallback_eltdx("券商返回空数据")
            if fb is not None:
                return fb
        return {"bars": res, "source": "broker", "cached_at": None}

    # 有缓存层：券商优先（命中缓存直接返，不触达回退）
    try:
        res = await cache.get_or_fetch(code, period, count, _fetch_broker, force=force)
    except Exception as exc:  # noqa: BLE001
        fb = await _fallback_eltdx(f"券商异常 {exc}")
        if fb is not None:
            return fb
        raise
    if not (res.get("bars") or []):
        fb = await _fallback_eltdx("券商返回空数据")
        if fb is not None:
            return fb
    return res


async def _persist(code: str, period: str, bars: list, adjust: str) -> None:
    """把 eltdx 取到的 K 线回补进本地缓存（幂等）：下次 auto 请求直接命中 cache，
    避免券商离线时反复打网络。"""
    cache = getattr(state, "kline_cache", None)
    if cache is None or not bars:
        return
    try:
        await cache.aput(code, period, bars, adjust=adjust or "")
        log.info("K线(eltdx)回补缓存: %s %s bars=%d", code, period, len(bars))
    except Exception as exc:  # noqa: BLE001
        log.warning("K线回补缓存失败 %s %s: %s", code, period, exc)


__all__ = ["get_bridge", "fetch_kline_cached", "BrokerError", "BrokerNotConnectedError"]
