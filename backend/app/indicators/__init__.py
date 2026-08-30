"""qmt_work 统一指标引擎包（G2-1）。

单一真源：指标定义 + 向量化实现只在本包存在一次，经 G2-2 自动暴露为 REST + MCP。
"""
from app.indicators.registry import (
    IndicatorParam,
    IndicatorSpec,
    calc,
    get_indicator,
    list_indicators,
    register,
)

__all__ = [
    "IndicatorParam", "IndicatorSpec", "register", "get_indicator",
    "list_indicators", "calc",
]
