"""兼容层：早期多源重构片段曾用 `app.datasource.manager.MarketDataHub` /
`get_hub` / `MarketDataUnavailable`。统一收敛到 `app.datasource.registry`
（DataSourceManager / get_manager / DataSourceUnavailable）后，这里仅做 re-export，
避免仍在引用旧名的调用方（如 routes/market.py）断链。新代码请直接用 registry。
"""
from app.datasource.registry import (  # noqa: F401
    DataSourceManager as MarketDataHub,
    DataSourceUnavailable as MarketDataUnavailable,
    get_manager as get_hub,
)

# 同时透传规范名，便于统一入口
from app.datasource.registry import (  # noqa: F401
    DataSourceManager,
    DataSourceUnavailable,
    get_manager,
    classify_board,
    limit_ratio,
)

__all__ = [
    "MarketDataHub", "MarketDataUnavailable", "get_hub",
    "DataSourceManager", "DataSourceUnavailable", "get_manager",
    "classify_board", "limit_ratio",
]
