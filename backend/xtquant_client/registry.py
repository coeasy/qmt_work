"""券商档案目录 + 适配器工厂（多券商 / 多客户端版本）。

每个券商档案描述：适配器实现、默认客户端路径、支持的账户类型、支持周期、所需 SDK、版本提示。
用户在前端「券商连接」页选择券商 + 填写客户端路径/账号，后端据此实例化对应适配器。

迅投系（国金/华鑫/银河/中信建投/兴业/广发 …）共用 `XTPQuantAdapter`，仅路径/账号不同；
同花顺/恒生PTrade/掘金 各有独立适配器（SDK 就绪后启用）。

from __future__ import annotations 使类型注解惰性求值，
避免类体内方法名（如 `list`）遮蔽内置 `list` 导致的注解求值错误。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .adapters.juejin import JuejinAdapter
from .adapters.ptrade import PTradeAdapter
from .adapters.ths import ThsAdapter
from .base import BrokerAdapter
from .xtp import XTPQuantAdapter


@dataclass
class BrokerProfile:
    id: str
    name: str
    adapter: str
    default_client_path: str = ""
    supported_account_types: list[str] = field(default_factory=lambda: ["STOCK"])
    supported_periods: list[str] = field(
        default_factory=lambda: ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"])
    sdk_required: str = ""
    min_version: str = ""
    multi_version: bool = True
    # P2 注册表 V2：能力协商 + 热插拔元数据
    capabilities: list[str] = field(
        default_factory=lambda: ["quote", "kline", "trade", "account", "positions"])
    features: dict = field(default_factory=dict)
    note: str = ""


# 内置券商档案（可扩展：新增券商只需在此追加一条 + 实现对应适配器）
BROKER_PROFILES: list[BrokerProfile] = [
    BrokerProfile(
        id="guojin", name="国金证券 QMT", adapter="xtp",
        default_client_path=r"C:\国金证券QMT交易端\userdata_mini",
        supported_account_types=["STOCK", "CREDIT", "OPTION", "FUTURES"],
        sdk_required="xtquant", min_version="迅投 xtquant",
        note="迅投 XTQuant，支持股票/信用/期权/期货账户"),
    BrokerProfile(
        id="huaxin", name="华鑫证券 奇点QMT", adapter="xtp",
        default_client_path=r"C:\华鑫证券\奇点QMT交易端\userdata_mini",
        supported_account_types=["STOCK", "CREDIT", "OPTION", "FUTURES"],
        sdk_required="xtquant", min_version="迅投 xtquant",
        note="华鑫（奇点）MiniQMT，迅投 XTQuant"),
    BrokerProfile(
        id="yinhe", name="银河证券 QMT", adapter="xtp",
        default_client_path=r"C:\银河证券QMT交易端\userdata_mini",
        supported_account_types=["STOCK", "CREDIT"],
        sdk_required="xtquant", min_version="迅投 xtquant", note="银河 MiniQMT"),
    BrokerProfile(
        id="zxjt", name="中信建投 QMT", adapter="xtp",
        default_client_path=r"C:\中信建投QMT交易端\userdata_mini",
        supported_account_types=["STOCK", "CREDIT"],
        sdk_required="xtquant", min_version="迅投 xtquant", note="中信建投 MiniQMT"),
    BrokerProfile(
        id="xy", name="兴业证券 QMT", adapter="xtp",
        default_client_path=r"C:\兴业证券QMT交易端\userdata_mini",
        supported_account_types=["STOCK", "CREDIT"],
        sdk_required="xtquant", min_version="迅投 xtquant", note="兴业 MiniQMT"),
    BrokerProfile(
        id="gf", name="广发证券 QMT", adapter="xtp",
        default_client_path=r"C:\广发证券QMT交易端\userdata_mini",
        supported_account_types=["STOCK", "CREDIT"],
        sdk_required="xtquant", min_version="迅投 xtquant", note="广发 MiniQMT"),
    BrokerProfile(
        id="ths", name="同花顺量化", adapter="ths",
        default_client_path="", supported_account_types=["STOCK"],
        sdk_required="ths_quant_sdk", min_version="同花顺量化终端",
        note="需安装同花顺量化 SDK"),
    BrokerProfile(
        id="ptrade", name="恒生 PTrade", adapter="ptrade",
        default_client_path="", supported_account_types=["STOCK", "CREDIT"],
        sdk_required="ptrade_sdk", min_version="PTrade 客户端",
        note="需安装恒生 PTrade SDK"),
    BrokerProfile(
        id="juejin", name="掘金量化", adapter="juejin",
        default_client_path="", supported_account_types=["STOCK", "FUTURES"],
        sdk_required="gm", min_version="掘金终端",
        note="需 pip install gm 并登录掘金终端"),
]

_PROFILE_MAP = {p.id: p for p in BROKER_PROFILES}


def get_profile(broker_id: str) -> BrokerProfile | None:
    return _PROFILE_MAP.get(broker_id)


def list_profiles() -> list[BrokerProfile]:
    return list(BROKER_PROFILES)


def create_adapter(broker_id: str, client_path: str, account_id: str,
                   account_type: str = "STOCK", session_id: int = 0,
                   min_version: str = "") -> BrokerAdapter:
    """依据券商档案实例化对应适配器（真实实现）。"""
    profile = get_profile(broker_id)
    if profile is None:
        raise ValueError(f"未知券商：{broker_id}")
    if profile.adapter == "xtp":
        # 计算运行时方案（进程内直连 / 桥接 / 无兼容运行时）
        plan = None
        abi_compat = False  # 仅在 ABI 兼容（或 xtquant 未定位）时退回进程内直连
        try:
            from .runtime import (xtp_runtime_plan, detect_xtquant_abis,
                                  host_python_minor, require_runtime_or_raise)
            from .xtp import _resolve_xtquant_path
            site = _resolve_xtquant_path(client_path or profile.default_client_path)
            broker_abis = detect_xtquant_abis(site)
            host = host_python_minor()
            abi_compat = (not broker_abis) or (host in broker_abis)
            if abi_compat:
                # 进程内直连安全，直接走 XTPQuantAdapter.start()
                return XTPQuantAdapter(
                    client_path=client_path or profile.default_client_path,
                    account_id=account_id, account_type=account_type,
                    session_id=session_id,
                    min_version=min_version or profile.min_version)
            # 主后端 ABI 不兼容：必须走桥接；无兼容运行时则在此给出清晰可操作提示
            plan = require_runtime_or_raise(
                site, prefer_bridge=True)
        except Exception as exc:  # noqa: BLE001
            from .base import BrokerSDKError
            # 非 ABINotSupportedError 也当作 SDK 错误向上抛，避免 3.13 上
            # 默默退回进程内后在 start() 触发「No module named xtquant.IPythonApiClient」
            msg = (str(exc)
                   if "xtquant 的 ABI 变体" in str(exc)
                   else f"券商 xtquant 不可用（运行时探测异常：{exc}）")
            raise BrokerSDKError("xtquant", msg) from exc
        if plan is not None:
            from .bridge_client import BridgeAdapter
            return BridgeAdapter(
                client_path=client_path or profile.default_client_path,
                account_id=account_id, account_type=account_type,
                session_id=session_id, min_version=min_version or profile.min_version,
                adapter="xtp", runtime=plan)
        # 兜底（理论上不可达）
        return XTPQuantAdapter(
            client_path=client_path or profile.default_client_path,
            account_id=account_id, account_type=account_type,
            session_id=session_id, min_version=min_version or profile.min_version)
    if profile.adapter == "ths":
        return ThsAdapter()
    if profile.adapter == "ptrade":
        return PTradeAdapter()
    if profile.adapter == "juejin":
        return JuejinAdapter()
    raise ValueError(f"未实现的适配器：{profile.adapter}")


# ---------------- 注册表 V2：能力协商 + 热插拔 ----------------
class Registry:
    """声明式券商目录（V2）：列表 / 能力协商 / 运行时热插拔新档案。

    默认档案来自内置 `BROKER_PROFILES`；运行期可通过 `register_profile`
    追加用户自定义券商（无需改代码、无需重启），实现热插拔。
    """

    def __init__(self):
        self._profiles: dict[str, BrokerProfile] = {}
        self.reload()

    def reload(self) -> int:
        """重置为内置档案集合（热插拔场景下重新加载基线）。"""
        self._profiles = {p.id: p for p in BROKER_PROFILES}
        return len(self._profiles)

    def register_profile(self, profile: BrokerProfile) -> str:
        """热插拔：追加/覆盖一条券商档案（运行时生效）。"""
        self._profiles[profile.id] = profile
        return profile.id

    def list(self) -> list[BrokerProfile]:
        return list(self._profiles.values())

    def get(self, broker_id: str) -> BrokerProfile | None:
        return self._profiles.get(broker_id)

    def effective_capabilities(self, profile: BrokerProfile) -> list[str]:
        """能力推导：基础能力 + 由账户类型派生的期权/信用/期货能力。"""
        caps = list(profile.capabilities)
        derived = {
            "OPTION": "option", "CREDIT": "credit", "FUTURES": "futures",
        }
        for at in profile.supported_account_types:
            d = derived.get(at)
            if d and d not in caps:
                caps.append(d)
        return caps

    def negotiate(self, broker_id: str, requested: list[str]) -> dict:
        """能力协商：返回券商支持 / 不支持的能力清单。"""
        profile = self.get(broker_id)
        if profile is None:
            return {"broker_id": broker_id, "found": False,
                    "supported": [], "unsupported": requested}
        caps = self.effective_capabilities(profile)
        req = requested or caps
        return {"broker_id": broker_id, "found": True,
                "supported": [c for c in req if c in caps],
                "unsupported": [c for c in req if c not in caps],
                "all_capabilities": caps}


# 全局注册表单例（模块级，路由/生命周期共享）
registry = Registry()


def list_profiles_v2() -> list[dict]:
    return [{
        "id": p.id, "name": p.name, "adapter": p.adapter,
        "supported_account_types": p.supported_account_types,
        "supported_periods": p.supported_periods,
        "sdk_required": p.sdk_required, "min_version": p.min_version,
        "multi_version": p.multi_version,
        "capabilities": registry.effective_capabilities(p),
        "features": p.features, "note": p.note,
    } for p in registry.list()]


def negotiate_capabilities(broker_id: str, requested: list[str]) -> dict:
    return registry.negotiate(broker_id, requested)


def hotplug_profile(payload: dict) -> BrokerProfile:
    """从字典热插拔一条券商档案（运行期新增券商）。"""
    p = BrokerProfile(
        id=str(payload.get("id") or "").strip(),
        name=payload.get("name", ""), adapter=payload.get("adapter", "xtp"),
        default_client_path=payload.get("default_client_path", ""),
        supported_account_types=payload.get("supported_account_types", ["STOCK"]),
        supported_periods=payload.get("supported_periods",
            ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"]),
        sdk_required=payload.get("sdk_required", ""),
        min_version=payload.get("min_version", ""),
        capabilities=payload.get("capabilities",
            ["quote", "kline", "trade", "account", "positions"]),
        features=payload.get("features", {}),
        note=payload.get("note", ""))
    if not p.id:
        raise ValueError("broker id 不能为空")
    registry.register_profile(p)
    return p
