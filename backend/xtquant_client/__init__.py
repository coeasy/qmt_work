"""券商客户端适配层：统一抽象 + 多券商实现 + 连接管理。

对外暴露：
- BrokerAdapter / BrokerError / BrokerNotConnectedError / BrokerSDKError
- XTPQuantAdapter（迅投系真实实现，覆盖国金/华鑫/银河等）
- BrokerManager（多连接管理）
- registry（券商档案 + 适配器工厂）

懒加载设计：包 ``__init__`` 只导入轻依赖（base），重依赖（manager -> app.db、
registry -> xtp 等）通过 PEP 562 ``__getattr__`` 按需加载。这样嵌入式桥接子进程
（backend/runtimes/cpXXX/python.exe，无 app 包）执行 ``python -m
xtquant_client.bridge_server`` 可正常启动；主进程 ``from xtquant_client import
BrokerManager`` 等用法保持不变。
"""
from .base import (BrokerAdapter, BrokerError, BrokerNotConnectedError,
                   BrokerSDKError)

__all__ = [
    "BrokerAdapter", "BrokerError", "BrokerNotConnectedError", "BrokerSDKError",
    "XTPQuantAdapter", "BrokerManager", "Connection", "ConnectionConfig",
    "BROKER_PROFILES", "create_adapter", "get_profile", "list_profiles",
]

# 名称 -> (模块, 属性)：仅在真正访问时才 import（避免把 app 依赖链带进桥接子进程）
_LAZY = {
    "XTPQuantAdapter": ("xtquant_client.xtp", "XTPQuantAdapter"),
    "BrokerManager": ("xtquant_client.manager", "BrokerManager"),
    "Connection": ("xtquant_client.manager", "Connection"),
    "ConnectionConfig": ("xtquant_client.manager", "ConnectionConfig"),
    "BROKER_PROFILES": ("xtquant_client.registry", "BROKER_PROFILES"),
    "create_adapter": ("xtquant_client.registry", "create_adapter"),
    "get_profile": ("xtquant_client.registry", "get_profile"),
    "list_profiles": ("xtquant_client.registry", "list_profiles"),
}


def __getattr__(name):  # PEP 562
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    mod = importlib.import_module(spec[0])
    return getattr(mod, spec[1])
