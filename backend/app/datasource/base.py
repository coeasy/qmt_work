"""数据源抽象层（DataSource）。

与 `xtquant_client/base.py` 的 `BrokerAdapter` 平行：BrokerAdapter 面向「券商直连 +
交易」，DataSource 面向「本地 / 第三方行情与基础数据补充源」（不提供交易）。

设计动机：当前行情架构是「纯券商直连 + 本地 K 线缓存」，无券商连接时实时盘口 /
基本数据全 503。引入 DataSource 作为 fallback，使系统在券商未连接时仍能通过 TDX 公共
行情等补充源获取行情与基础数据，而不伪造任何数据（遵循项目「零 mock」铁律）。
"""
from abc import ABC, abstractmethod
from typing import Optional


class DataSource(ABC):
    """行情 / 基础数据补充源（无交易能力）。"""

    #: 数据源标识，用于返回体的 source 字段与前端展示
    name: str = "base"

    @abstractmethod
    async def get_quote(self, code: str) -> dict:
        """实时盘口快照，返回结构与 `xtp._norm_quote` 一致：
        {code, last, open, high, low, lastClose, volume, amount,
         bid, ask, bid_vol, ask_vol, bids:[{price,volume}], asks:[{price,volume}], ts}
        """

    @abstractmethod
    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                       adjust: Optional[str] = None) -> list:
        """历史 K 线，返回 [{time, open, high, low, close, volume, amount}, ...]。"""

    @abstractmethod
    async def get_instrument_detail(self, code: str) -> dict:
        """合约基础信息 {name, exchange, high_limit, low_limit, pre_close}。"""

    @abstractmethod
    async def get_stock_list(self) -> list:
        """全市场股票列表 [{code, name, category}, ...]。"""


__all__ = ["DataSource"]
