"""数据源包：行情 / 基础数据的本地与第三方补充源（不提供交易）。

统一导出 DataSource 抽象、板块归类工具，以及多源编排中心
`DataSourceManager`（见 app.datasource.registry）。
"""
from datasource.base import DataSource
from datasource.board import classify_board, limit_ratio
from datasource.degrade import envelope, local_bars, local_boards, local_stock_list
from datasource.local_store import LocalStore, get_store
from datasource.models import (
    Bar,
    BoardItem,
    BoardKline,
    EtfInfo,
    InstrumentInfo,
    Moneyflow,
    MoneyflowPoint,
    Quote,
    QuoteLevel,
    StockInfo,
)
from datasource.providers import ProviderCatalog, ProviderDescriptor, provider_catalog
from datasource.registry import (
    DataSourceManager,
    DataSourceUnavailable,
    MarketDataHub,
    MarketDataUnavailable,
    UnsupportedDataSource,
    get_hub,
    get_manager,
)
from datasource.result import DataResult
from datasource.snapshots import DatasetSnapshotStore, ReconcileResult, reconcile_bars

__all__ = [
    "DataSource", "classify_board", "limit_ratio",
    "DataSourceManager", "DataSourceUnavailable", "UnsupportedDataSource", "get_manager",
    "get_hub", "MarketDataHub", "MarketDataUnavailable",
    # G1 统一数据契约
    "Quote", "QuoteLevel", "Bar", "InstrumentInfo", "StockInfo",
    "BoardItem", "EtfInfo", "BoardKline", "Moneyflow", "MoneyflowPoint",
    "DataResult",
    "DatasetSnapshotStore", "ReconcileResult", "reconcile_bars",
    "ProviderCatalog", "ProviderDescriptor", "provider_catalog",
    # G1-4 本地数据仓
    "LocalStore", "get_store",
    # G1-6 降级策略
    "local_bars", "local_stock_list", "local_boards", "envelope",
]
