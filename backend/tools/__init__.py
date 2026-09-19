"""工具层：REST / MCP 两端复用的 Python 函数（§4.1）。

所有工具通过 `get_bridge(conn_id)` 取得当前/指定券商连接的线程桥，
统一经 `bridge.call(...)` 调用真实券商 SDK，绝不产生假数据。
K 线在无券商连接时回退到 eltdx(TDX 公共行情) 数据源。
"""
import logging

from core.state import state
from datasource.registry import get_hub
from xtquant_client.base import BrokerError, BrokerNotConnectedError

log = logging.getLogger("qmt_work.tools")


def get_bridge(conn_id: str | None = None):
    """返回指定/活跃券商连接 bridge；无连接时抛 BrokerNotConnectedError。

    V11 R5：**委托** :meth:`core.state.AppState.require_bridge`。
    此前这里是同一逻辑的第二份实现，且两处都会在 ``broker_manager`` 为 None 时抛
    ``AttributeError``（而非契约声明的 ``BrokerNotConnectedError``）—— 后果是
    ``POST /factors/from-kline`` 在无券商时返回 500，而契约要求 503
    （tests/test_factors.py::test_from_kline_without_broker_contract）。
    """
    return state.require_bridge(conn_id)


async def fetch_kline_cached(code: str, period: str = "1d", count: int = 250,
                             broker_id: str | None = None, force: bool = False,
                             source: str = "auto", adjust: str | None = None) -> dict:
    """C1：缓存优先取历史 K 线。

    返回 {"bars": [...], "source": "cache"|"broker"|"eltdx"|"cache_stale", "cached_at": ts}。
    缓存未初始化时直接回源。source=auto 时券商优先，无连接/异常回退 eltdx(TDX 公共行情)。
    adjust: qfq/hfq/''。**显式复权优先走 eltdx**（少一次券商 RPC，且 eltdx 原生支持复权）；
    但券商路径同样会透传 adjust（V11 R14 修复前漏传，导致降级时口径静默变成不复权）。

    ⚠️ 两条关键不变量（曾经因此出现「图表空白但无任何报错」）：
    1. 代码规范化：券商（xtquant）**只认 `600519.SH` 形态**。传裸代码 `600519`
       会静默返回空列表——不抛异常、不报错，于是既不走 except 分支也不回退
       eltdx，最终 K 线 0 根。入口统一补交易所后缀（幂等）。
    2. 空结果 = 失败：券商返回空列表属于「成功但无数据」，同样必须回退 eltdx，
       否则用户看到空白图表而无任何错误提示。
    """
    from datasource.instrument import with_exchange_suffix
    code = with_exchange_suffix(code)
    # 缓存编排也必须遵守 source 契约；否则未知 source 会落入默认券商路径，
    # 或显式券商请求在复权失败时被错误改成 TDX。
    source = get_hub().validate_source(source)
    # 缓存编排也必须遵守 source 契约；否则未知 source 会落入默认券商路径，
    # 或显式券商请求在复权失败时被错误改成 TDX。
    source = get_hub().validate_source(source)

    async def _fetch_broker(c: str, p: str, n: int):
        """回源券商取 K 线。

        ★ 必须把 ``adjust`` 传下去（V11 R14 修复）。此前这里只传 3 个位置参数，
        而适配器的 ``get_kline(code, period, count, start, end, adjust)`` 第 6 个
        参数才是复权口径 —— 不传即恒为 ``None`` ⇒ ``dividend_type="none"``
        ⇒ **拿到未复权价**。后果（实测确认）：

        1. 上层 ``kline_cache.get_or_fetch`` 仍按**请求口径**落库（``aput(..., adjust="qfq")``），
           响应 ``market.py`` 也回 ``"adjust":"qfq"``，前端角标显示「前复权」；
        2. 于是用户看到的是**标着前复权的不复权价**，除权日出现巨大跳空，
           形态识别与指标全部失真；
        3. 只有在线源（eltdx/腾讯）恰好返回 qfq 时才「看起来正常」，
           一旦在线源失败降级到券商，口径就悄悄变了。
        """
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_kline, c, p, n, "", "", adjust or "")

    async def _fetch_eltdx(c: str, p: str, n: int):
        bars, src = await get_hub().get_kline(
            c, p, n, source="eltdx", conn_id=broker_id, adjust=adjust)
        return bars, src

    # auto/TDX 的显式复权走支持复权的源；显式 broker 不得静默改源。
    if source in ("auto", "eltdx") and adjust in ("qfq", "hfq") and period.lower() in ("1d", "day", "1w", "week", "1mon", "mon", "month"):
        try:
            bars, src = await _fetch_eltdx(code, period, count)
        except Exception as exc:  # noqa: BLE001
            if source == "eltdx":
                raise
            log.warning("eltdx 复权K线失败，auto 链继续尝试券商: %s", exc)
        else:
            # ★ 空结果 = 失败（与本文件 docstring 的不变量一致，V11 R14）：
            # 此前 eltdx 返回空（**不抛异常**）时会在这里直接 return，
            # 于是 auto 链**从不回退券商** —— 实测表现为请求 `adj=qfq` 时
            # 返回 source=None 且 0 根，路由随即降级到 local:sqlite，
            # 用户拿到的既不是券商 qfq 也不是在线源，而是本地可能很旧的数据。
            # 现在空结果与异常同路：继续往券商走（券商已支持 dividend_type）。
            if bars:
                await _persist(code, period, bars, adjust)
                return {"bars": bars, "source": src, "cached_at": None}
            if source == "eltdx":
                # 显式指定 eltdx 时不得改源：如实返回空，由路由决定兜底。
                return {"bars": [], "source": src, "cached_at": None}
            log.warning("eltdx 复权K线返回空（未报错），auto 链继续尝试券商：%s", code)

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

    # kline_cache 不含 provider 维度；显式 broker 不能命中此前由 TDX 写入的缓存。
    if cache is None or source == "broker":
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
        res = await cache.get_or_fetch(code, period, count, _fetch_broker,
                                       force=force, adjust=adjust or "")
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
