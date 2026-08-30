"""兼容层：早期多源重构片段曾用 `app.datasource.manager.MarketDataHub` /
`get_hub` / `MarketDataUnavailable`。统一收敛到 `app.datasource.registry`
（DataSourceManager / get_manager / DataSourceUnavailable）后，这里仅做 re-export，
避免仍在引用旧名的调用方（如 routes/market.py）断链。新代码请直接用 registry。
"""
# 同时透传规范名，便于统一入口
from app.datasource.registry import (  # noqa: F401
    DataSourceManager,
    DataSourceUnavailable,
    classify_board,
    get_manager,
    limit_ratio,
)
from app.datasource.registry import (  # noqa: F401
    DataSourceManager as MarketDataHub,
)
from app.datasource.registry import (
    DataSourceUnavailable as MarketDataUnavailable,
)
from app.datasource.registry import (
    get_manager as get_hub,
)

__all__ = [
    "MarketDataHub", "MarketDataUnavailable", "get_hub",
    "DataSourceManager", "DataSourceUnavailable", "get_manager",
    "classify_board", "limit_ratio",
]
