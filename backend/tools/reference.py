"""参考数据类工具：交易日历 / 板块列表 / 板块成分 / 财务摘要 / Level-2 逐笔。

全部经 BrokerAdapter 真实调用（xtquant xtdata 等），未连接或无数据权限时返回明确错误。
"""
from . import get_bridge


def register_reference_tools(mcp):
    @mcp.tool()
    async def trading_calendar(start: str = "", end: str = "", broker_id: str = "") -> list[str]:
        """获取交易日历（start/end 格式 YYYYMMDD，留空为最近一段）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_trading_calendar, start, end)

    @mcp.tool()
    async def sector_list(broker_id: str = "") -> list[str]:
        """获取券商行情端支持的板块列表（沪深A股/ETF/指数/行业等）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_sector_list)

    @mcp.tool()
    async def sector_stocks(sector: str = "沪深A股", broker_id: str = "") -> list[str]:
        """获取板块成分股代码列表。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_sector_stocks, sector)

    @mcp.tool()
    async def financial_summary(code: str, broker_id: str = "") -> dict:
        """获取个股财务摘要（每股收益/净资产/营收/利润/ROE 等，最近报告期）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_financial, code)

    @mcp.tool()
    async def l2_transactions(code: str, count: int = 100, broker_id: str = "") -> list[dict]:
        """获取 Level-2 逐笔成交明细（需 L2 数据权限）。"""
        b = get_bridge(broker_id or None)
        return await b.call(b.gateway.get_l2_transactions, code, count)
