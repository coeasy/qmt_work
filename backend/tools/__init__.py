"""工具层：REST / MCP / Agent 三端复用的 Python 函数（§4.1）。

所有工具通过 `get_bridge(conn_id)` 取得当前/指定券商连接的线程桥，
统一经 `bridge.call(...)` 调用真实券商 SDK，绝不产生假数据。
"""
from app.state import state
from xtquant_client.base import BrokerError, BrokerNotConnectedError


def get_bridge(conn_id: str | None = None):
    """返回指定/活跃券商连接 bridge；无连接时抛 BrokerNotConnectedError。"""
    b = state.broker_manager.bridge(conn_id)
    if b is None:
        raise BrokerNotConnectedError(
            "当前未连接任何券商客户端：请到「券商连接」页添加并连接券商（国金/华鑫/银河等 MiniQMT）。")
    return b


async def fetch_kline_cached(code: str, period: str = "1d", count: int = 250,
                             broker_id: str | None = None, force: bool = False) -> dict:
    """C1：缓存优先取历史 K 线。

    返回 {"bars": [...], "source": "cache"|"broker"|"cache_stale", "cached_at": ts}。
    缓存未初始化时直接回源券商（source=broker）。
    """
    async def _fetch(c: str, p: str, n: int):
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_kline, c, p, n)

    cache = getattr(state, "kline_cache", None)
    if cache is None:
        return {"bars": await _fetch(code, period, count), "source": "broker",
                "cached_at": None}
    return await cache.get_or_fetch(code, period, count, _fetch, force=force)


__all__ = ["get_bridge", "fetch_kline_cached", "BrokerError", "BrokerNotConnectedError"]
