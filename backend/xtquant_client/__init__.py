"""券商客户端适配层：统一抽象 + 多券商实现 + 连接管理。

对外暴露：
- BrokerAdapter / BrokerError / BrokerNotConnectedError / BrokerSDKError
- XTPQuantAdapter（迅投系真实实现，覆盖国金/华鑫/银河等）
- BrokerManager（多连接管理）
- registry（券商档案 + 适配器工厂）
"""
from .base import (BrokerAdapter, BrokerError, BrokerNotConnectedError,
                   BrokerSDKError)
from .manager import BrokerManager, Connection, ConnectionConfig
from .registry import BROKER_PROFILES, create_adapter, get_profile, list_profiles
from .xtp import XTPQuantAdapter

__all__ = [
    "BrokerAdapter", "BrokerError", "BrokerNotConnectedError", "BrokerSDKError",
    "XTPQuantAdapter", "BrokerManager", "Connection", "ConnectionConfig",
    "BROKER_PROFILES", "create_adapter", "get_profile", "list_profiles",
]
