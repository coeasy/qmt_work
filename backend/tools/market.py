"""行情类工具：get_quote / get_full_tick / get_kline / get_tick / get_stock_list / search_stocks。

全部走真实券商 SDK（XTQuant xtdata 等）。
"""
from . import fetch_kline_cached, get_bridge


def register_market_tools(mcp):
    @mcp.tool()
    async def get_quote(code: str, broker_id: str = "") -> dict:
        """获取实时行情快照（最新价/买卖价/成交量）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_quote, code)

    @mcp.tool()
    async def get_full_tick(codes: list[str], broker_id: str = "") -> dict:
        """获取多标的五档盘口全推快照。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_full_tick, codes)

    @mcp.tool()
    async def get_kline(code: str, period: str = "1d", count: int = 100,
                       start: str = "", end: str = "", broker_id: str = "") -> list[dict]:
        """获取历史 K 线（period: 1m/5m/15m/30m/60m/1d/1w/1mon；count: 数量）。

        未指定 start/end 时走本地缓存（C1），命中不穿透券商客户端。
        """
        if start or end:
            b = get_bridge(broker_id or None)
            return await b.call(b.gateway.get_kline, code, period, count, start, end)
        res = await fetch_kline_cached(code, period, count, broker_id=broker_id or None)
        return res.get("bars") or []

    @mcp.tool()
    async def get_tick(code: str, broker_id: str = "") -> dict:
        """获取最新逐笔/快照。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_tick, code)

    @mcp.tool()
    async def get_stock_list(sector: str = "沪深A股", broker_id: str = "") -> list[dict]:
        """获取板块股票列表（沪深A股/ETF/指数等）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_stock_list, sector)

    @mcp.tool()
    async def search_stocks(keyword: str, limit: int = 20, broker_id: str = "") -> list[dict]:
        """按代码/名称关键字搜索股票。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.search_stocks, keyword, limit)
