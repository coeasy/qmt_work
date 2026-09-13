"""平台运行时能力自描述（D-C / D12）。

把「交易可用性」「选股可用性」等运行态能力收敛到单一来源，供 REST / MCP / 前端统一消费。
核心修复：
- ``can("trading")`` 原先恒为 false：本模块依据真实券商连接态返回 ``trading`` /
  ``realtime_trading``，前端 PlatformContext 据此联动，未连接券商时正确地显示「不可交易」
  而非「永远不可交易」。
- ``screening_ready``（D12 底线）：只要「QMT 已连接」或「任一非券商源（eltdx /
  baostock / akshare / pytdx）依赖可用」或「本地 canonical 层已有数据」任一成立，选股
  即可用；三者皆无才为 false（此时选股接口必须返回 503 + 安装指引，禁止返回空列表）。
"""
from __future__ import annotations

import os
from typing import Any


def _broker_connected_count() -> int:
    try:
        from core.state import state
        mgr = getattr(state, "broker_manager", None)
        if mgr is None:
            return 0
        conns = mgr.all_connections()
        return sum(1 for c in conns if getattr(c, "connected", False))
    except Exception:  # noqa: BLE001
        return 0


def _commercial_mode() -> bool:
    try:
        from datasource.registry import get_manager
        return get_manager()._commercial_mode
    except Exception:  # noqa: BLE901
        return os.environ.get("QMT_COMMERCIAL") == "1"


def _catalog_describe() -> list[dict]:
    try:
        from datasource.providers import provider_catalog
        return provider_catalog.describe()
    except Exception:  # noqa: BLE001
        return []


def _has_local_data() -> bool:
    """本地 canonical 层是否已有可消费数据（无网络源的末位兜底）。"""
    try:
        from core.state import state
        db = getattr(state, "db", None)
        if db is None:
            return False
        # 轻量探测：local_bars / canonical_bars 是否存在且非空
        for tbl in ("canonical_bars", "local_bars"):
            try:
                n = db.query_one(f"SELECT COUNT(*) AS n FROM {tbl}")
                if n and (n.get("n") or 0) > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def get_platform_status() -> dict[str, Any]:
    """返回平台能力 + 数据源可用性快照（D-C 契约结构）。"""
    connected = _broker_connected_count()
    commercial = _commercial_mode()
    catalog = _catalog_describe()

    # 选股可用源：QMT 已连接 + 任一 kline 能力非券商源依赖可用（且商用许可）。
    screening_providers: list[str] = []
    if connected > 0:
        screening_providers.append("qmt")
    for d in catalog:
        pid = d.get("provider")
        if pid == "broker":
            continue
        if not d.get("dependency_available"):
            continue
        if commercial and not d.get("commercial_ok", True):
            continue
        if "kline" in d.get("capabilities", ()):
            screening_providers.append(pid)
    has_local = _has_local_data()
    screening_ready = len(screening_providers) > 0 or has_local

    # 链路解析（运行态）：结合注册态 + 依赖 + 商用过滤后的实际链
    chain_resolved: dict[str, list[str]] = {}
    try:
        from datasource.providers import provider_catalog, DEFAULT_CAPABILITY_CHAINS
        from datasource.registry import get_manager
        registered = set(get_manager().list_sources())
        for cap in DEFAULT_CAPABILITY_CHAINS:
            chain_resolved[cap] = provider_catalog.resolve_chain(
                cap, commercial_mode=commercial, registered=registered)
    except Exception:  # noqa: BLE001
        pass

    return {
        "platform": "qmt_work",
        "capabilities": {
            "research": True, "data": True, "factors": True, "backtest": True,
            "paper": True,
            "trading": connected > 0,
            "realtime_trading": connected > 0,
        },
        "execution": {
            "available": connected > 0,
            "connected_count": connected,
            "active_conn_id": _active_conn_id(),
        },
        "sources": {
            "available": len(catalog) > 0,
            "chain_version": "chain.v1",
            "default_source_policy": "auto",
            "commercial_mode": commercial,
            "capability_chains": dict(DEFAULT_CAPABILITY_CHAINS) if _chains() else {},
            "chain_resolved": chain_resolved,
            "providers": catalog,
            "screening_ready": screening_ready,
            "screening_providers": screening_providers,
            "local_data_available": has_local,
        },
        "reasons": {
            "trading": "no_broker_connected" if connected == 0 else "ok",
            "screening": (None if screening_ready
                          else "no_data_source_and_no_local_data"),
        },
    }


def _active_conn_id() -> Any:
    try:
        from core.state import state
        mgr = getattr(state, "broker_manager", None)
        if mgr is None:
            return None
        active = mgr.active_bridge()
        return getattr(active, "conn_id", None) if active else None
    except Exception:  # noqa: BLE001
        return None


def _chains() -> bool:
    try:
        from datasource.providers import DEFAULT_CAPABILITY_CHAINS
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["get_platform_status"]
