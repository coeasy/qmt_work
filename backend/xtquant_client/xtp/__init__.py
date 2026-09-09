"""迅投 XTQuant 适配器（覆盖所有基于迅投 MiniQMT 的券商客户端）。

支持券商（同一套 xtquant SDK，仅 client_path / account_id / account_type / session_id 不同）：
- 国金证券 QMT、华鑫证券（奇点/华鑫 QMT）、银河证券、中信建投、兴业、广发等所有迅投系 MiniQMT。

xtquant 包自动发现（无需用户手动安装）：
- 若当前 Python 环境已装（pip install 过），直接使用；
- 否则按 client_path（userdata_mini 目录）推断客户端根目录，自动把
  <根>/bin.x64/Lib/site-packages 等目录注入 sys.path 加载客户端自带的 xtquant，
  并缓存结果（版本随客户端升级自动同步，不随程序分发专有包）。

（拆分说明）本文件现为**薄壳兼容层**：实现按职责拆分至 xtquant_client/xtp/ 包
（_common / env / quotes / trading / account / instrument / adapter），代码逐行
原样搬移、行为不变。所有既有导入（``from xtquant_client.xtp import X``）与
测试 monkeypatch 目标（如 ``monkeypatch.setattr(xtp_mod, "_probe_quote_service", ...)``、
``xtp_mod._ensure_xtconstant = ...``）经本壳 re-export 继续生效：壳模块属性即
原函数对象；子模块内部对可 patch 符号的调用一律经 ``_common._shell_attr`` 在
壳模块上动态查找，保证补丁可见。
"""
# This package is intentionally a compatibility re-export shell. The star
# imports preserve old import paths and monkeypatch targets; F405 is expected
# for the names listed in the explicit public surface below.
# ruff: noqa: F405
import logging  # noqa: F401  兼容原模块属性面
import os  # noqa: F401
import re  # noqa: F401
import sys  # noqa: F401
import threading  # noqa: F401
from datetime import datetime  # noqa: F401

from ..base import (  # noqa: F401  兼容原模块属性面
    BrokerAdapter,
    BrokerError,
    BrokerNotConnectedError,
    BrokerSDKError,
)
from ._common import *  # noqa: F401,F403
from .account import *  # noqa: F401,F403
from .adapter import *  # noqa: F401,F403
from .env import *  # noqa: F401,F403
from .instrument import *  # noqa: F401,F403
from .quotes import *  # noqa: F401,F403
from .trading import *  # noqa: F401,F403

__all__ = [
    'log',
    '_ACCOUNT_CLASS',
    '_ORDER_OP',
    '_PRICE_TYPE',
    '_XTQUANT_CACHE',
    '_XTQUANT_REL',
    '_normalize',
    '_candidate_roots',
    '_is_likely_root',
    '_SYSTEM_DIR_NAMES',
    '_is_system_dir',
    '_resolve_xtquant_path',
    '_load_xtquant_from',
    '_ensure_xtconstant',
    '_TRADER_API_CACHE',
    '_load_trader_api',
    '_pick',
    '_dget',
    '_normalize_kline_period',
    '_BUY_ORDER_TYPES',
    '_SELL_ORDER_TYPES',
    '_direction_from_order_type',
    '_shell_attr',
    'probe_environment',
    '_probe_xtdata',
    '_scan_qmt_listeners',
    '_probe_quote_service',
    '_FULL_EXE_NAMES',
    '_MINI_EXE_NAMES',
    '_QUOTE_EXE_NAMES',
    '_running_client_exes',
    '_latest_login_log',
    '_find_client_exe',
    'launch_client',
    '_effective_trade_dir',
    'QmtCapabilities',
    'QmtVersionProfile',
    '_VERSION_RE',
    '_detect_version_str',
    '_detect_sdk_version',
    '_infer_capabilities',
    'build_version_profile',
    'QuotesMixin',
    'TradingMixin',
    'AccountMixin',
    'InstrumentMixin',
    'XTPQuantAdapter',
]
