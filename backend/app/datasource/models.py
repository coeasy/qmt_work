"""qmt_work 统一数据契约 · 标准模型层（G1-1）。

来源借鉴：OpenBB `standard_models` —— 每个数据域定义**一个**标准模型，各 Provider
（券商直连 / eltdx / 未来 akshare / tushare / eastmoney…）通过字段别名
（`validation_alias`）把自有字段映射到标准模型，从而「换源即一致、前端不再各写适配」。

设计约定（与现有代码对齐）：
- 输入口径：行情/基础数据真实返回多为 **camelCase**（见 `eltdx_source.py`
  `get_quote`/`get_kline`、各 `market.py` 路由）。因此字段的 `validation_alias`
  优先匹配 camelCase 源键，同时保留 snake_case 别名（`populate_by_name=True`）。
- 输出口径：默认 `model_dump()` 为 snake_case，供内部/选股/指标消费；需要对接
  前端 camelCase 时显式 `model_dump(by_alias=True)`。本批次**不改变任何路由现有
  输出**（路由仍返回原始 dict），模型仅作为「归一化与契约校验」的单一真源。
- 缺失字段一律 `None`，绝不估算填充（零 mock 铁律）。

> 协议红线：仅借鉴 OpenBB 的「标准模型 + 别名映射」接口范式，所有实现为 qmt_work
> 独立编写，不复制任何 AGPL 代码。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _AliasModel(BaseModel):
    """所有标准模型的基类：允许 camelCase / snake_case 双向填充。"""

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# 盘口 / 报价
# ---------------------------------------------------------------------------
class QuoteLevel(_AliasModel):
    """五档盘口单项 {price, volume}。"""

    price: Optional[float] = None
    volume: Optional[float] = Field(default=None, validation_alias=AliasChoices("volume", "vol"))


class Quote(_AliasModel):
    """实时盘口快照（对齐 `xtp._norm_quote` 与 `eltdx_source.get_quote`）。

    字段语义单一真源：
    - ``last`` 最新价；``last_close`` 昨收；``change``/``change_pct`` 由 last 与
      last_close 真实计算（非估算），分母为 0 时 None。
    - ``volume`` 成交量（手）；``amount`` 成交额（元）。
    - ``bids``/``asks`` 五档 [{price, volume}]。
    """

    code: str
    name: Optional[str] = None
    last: Optional[float] = Field(default=None, validation_alias=AliasChoices("last", "last_price"))
    open: Optional[float] = Field(default=None, validation_alias=AliasChoices("open", "open_price"))
    high: Optional[float] = Field(default=None, validation_alias=AliasChoices("high", "high_price"))
    low: Optional[float] = Field(default=None, validation_alias=AliasChoices("low", "low_price"))
    last_close: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("lastClose", "pre_close", "preclose", "last_close")
    )
    volume: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("volume", "total_hand", "vol")
    )
    amount: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_vol: Optional[float] = None
    ask_vol: Optional[float] = None
    bids: Optional[List[QuoteLevel]] = None
    asks: Optional[List[QuoteLevel]] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    # 通达信风格盘口扩展（内外盘 / 现量 / 委买委卖总量），缺失即 None
    inside: Optional[float] = None
    outside: Optional[float] = None
    current_hand: Optional[float] = None
    sum_buy_vol: Optional[float] = None
    sum_sell_vol: Optional[float] = None
    ts: Optional[str] = None


# ---------------------------------------------------------------------------
# K 线
# ---------------------------------------------------------------------------
class Bar(_AliasModel):
    """单根 K 线（对齐 `eltdx_source.get_kline` 与 `xtp` 历史 K 线）。

    ``volume`` 单位随源：券商直连为「股」，eltdx 为「手」。归一化到「股」属于
    消费层契约（见 niuniu 项目的 FallbackRealtimeSource 经验），本模型不臆造单位，
    仅如实承载源值，由消费方按源声明转换。
    """

    time: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    amount: Optional[float] = None


# ---------------------------------------------------------------------------
# 合约基础信息
# ---------------------------------------------------------------------------
class InstrumentInfo(_AliasModel):
    """合约基础信息（对齐 `DataSource.get_instrument_detail`）。

    注意：真实源（券商直连 / eltdx）的合约详情返回**不含 code**
    （契约仅 {name, exchange, high_limit, low_limit, pre_close}），故 ``code``
    为可选——由 ``DataSource.get_instrument_model`` 在消费侧用已知 code 注入，
    避免模型臆造源不产生的字段。
    """

    code: Optional[str] = None
    name: Optional[str] = None
    exchange: Optional[str] = None
    high_limit: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("high_limit", "upper_limit", "zt_price")
    )
    low_limit: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("low_limit", "lower_limit", "dt_price")
    )
    pre_close: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("pre_close", "preclose", "lastClose")
    )


# ---------------------------------------------------------------------------
# 列表类（股票 / 板块 / ETF）
# ---------------------------------------------------------------------------
class StockInfo(_AliasModel):
    """全市场股票列表单项（对齐 `DataSource.get_stock_list`）。"""

    code: str
    name: Optional[str] = None
    category: Optional[str] = None


class BoardItem(_AliasModel):
    """板块榜 / ETF 清单单项（对齐 `market.py` boards / etfs 返回）。

    ``kind`` 对板块为 industry/concept/stat；对 ETF 为 None。
    ``last``/``change_pct``/``amount`` 在含实时价时填充，否则 None。
    """

    code: str
    name: Optional[str] = None
    kind: Optional[str] = None
    last: Optional[float] = None
    change_pct: Optional[float] = None
    amount: Optional[float] = None


class EtfInfo(BoardItem):
    """ETF 清单单项（字段与 BoardItem 一致，独立命名便于契约可读）。"""


class BoardKline(_AliasModel):
    """板块 / 指数 K 线（对齐 `market.py` board/kline 返回）。"""

    code: str
    period: Optional[str] = None
    bars: List[Bar] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 资金流
# ---------------------------------------------------------------------------
class MoneyflowPoint(_AliasModel):
    """资金流强度序列点 {t, buy, sell}。"""

    t: Optional[str] = None
    buy: Optional[float] = None
    sell: Optional[float] = None


class Moneyflow(_AliasModel):
    """个股 / 板块资金流（对齐 `market.py` moneyflow 返回）。

    任一字段缺失即 None，由前端显式显示「—」，禁止估算填充。
    """

    code: str
    inside: Optional[float] = None
    outside: Optional[float] = None
    net: Optional[float] = None
    strength: Optional[List[MoneyflowPoint]] = None
    volume_ratio: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("volume_ratio", "vr")
    )
    est: Optional[float] = None
    ts: Optional[str] = None


__all__ = [
    "Quote",
    "QuoteLevel",
    "Bar",
    "InstrumentInfo",
    "StockInfo",
    "BoardItem",
    "EtfInfo",
    "BoardKline",
    "Moneyflow",
    "MoneyflowPoint",
]
