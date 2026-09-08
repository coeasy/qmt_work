"""数据源抽象层（DataSource）。

与 `xtquant_client/base.py` 的 `BrokerAdapter` 平行：BrokerAdapter 面向「券商直连 +
交易」，DataSource 面向「本地 / 第三方行情与基础数据补充源」（不提供交易）。

设计动机：当前行情架构是「纯券商直连 + 本地 K 线缓存」，无券商连接时实时盘口 /
基本数据全 503。引入 DataSource 作为 fallback，使系统在券商未连接时仍能通过 TDX 公共
行情等补充源获取行情与基础数据，而不伪造任何数据（遵循项目「零 mock」铁律）。
"""
from abc import ABC, abstractmethod
from typing import ClassVar, List, Optional

from datasource.models import (
    Bar,
    InstrumentInfo,
    Quote,
    StockInfo,
)


class DataSource(ABC):
    """行情 / 基础数据补充源（无交易能力）。

    G1-2（TET 抽象增强，向后兼容）：在保留 4 个既有抽象方法的前提下，新增
    ``alias_dict`` 与 ``get_*_model`` 默认实现——任何子类**无需改动**即可把原始
    dict 经标准模型（`app.datasource.models`）归一化为单一真源对象。后续子批次可将
    ``eltdx_source`` 等重写为显式三段式（``transform_query → extract_data →
    transform_data``），本基类已预留钩子位置，不影响现有直连实现。
    """

    #: 数据源标识，用于返回体的 source 字段与前端展示
    name: str = "base"

    #: 字段别名映射（源字段 -> 标准模型字段）。子类可覆盖以适配自有命名。
    #: 例：{"last_price": "last"} 表示源用 last_price 承载最新价。
    alias_dict: ClassVar[dict] = {}

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

    # ------------------------------------------------------------------
    # G1-2 标准模型访问器（默认实现，子类免费获得；可重写为三段式）
    # ------------------------------------------------------------------
    async def get_quote_model(self, code: str) -> Optional[Quote]:
        """报价 → 标准模型 ``Quote``。原始为 None 时返回 None（绝不以空模型冒充）。"""
        raw = await self.get_quote(code)
        if not raw:
            return None
        return Quote.model_validate(raw)

    async def get_kline_models(
        self, code: str, period: str = "1d", count: int = 250, adjust: Optional[str] = None
    ) -> List[Bar]:
        """历史 K 线 → 标准模型 ``Bar`` 列表。"""
        raw = await self.get_kline(code, period=period, count=count, adjust=adjust)
        if not raw:
            return []
        return [Bar.model_validate(b) for b in raw]

    async def get_instrument_model(self, code: str) -> Optional[InstrumentInfo]:
        """合约基础信息 → 标准模型 ``InstrumentInfo``。

        源返回不含 code（见契约），此处用调用方已知 code 注入，保证模型可溯源。
        """
        raw = await self.get_instrument_detail(code)
        if not raw:
            return None
        return InstrumentInfo.model_validate({"code": code, **raw})

    async def get_stock_list_models(self) -> List[StockInfo]:
        """全市场股票列表 → 标准模型 ``StockInfo`` 列表。"""
        raw = await self.get_stock_list()
        if not raw:
            return []
        return [StockInfo.model_validate(s) for s in raw]


__all__ = ["DataSource"]
