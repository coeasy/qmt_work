"""数据源包：行情 / 基础数据的本地与第三方补充源（不提供交易）。

统一导出 DataSource 抽象、板块归类工具，以及多源编排中心
`DataSourceManager`（见 app.datasource.registry；app.datasource.manager 为兼容 shim）。
"""
from app.datasource.base import DataSource
from app.datasource.board import classify_board, limit_ratio
from app.datasource.registry import (
    DataSourceManager, DataSourceUnavailable, get_manager,
    get_hub, MarketDataHub, MarketDataUnavailable,
)

__all__ = [
    "DataSource", "classify_board", "limit_ratio",
    "DataSourceManager", "DataSourceUnavailable", "get_manager",
    "get_hub", "MarketDataHub", "MarketDataUnavailable",
]
