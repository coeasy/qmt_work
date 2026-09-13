"""G4 统一数据面：TopicPolicy 策略表（后端下发，杜绝前后端漂移）。

前端数据总线（lib/dataHub.js）以本策略表为**单一真源**：ttl_ms（快照缓存寿命）/
min_interval_ms（同 topic 最小请求间隔）/ coalesce_within_ms（合并窗口，多个订阅
在窗口内合并为一次网络请求）/ priority / stale_ok（陈旧快照可先行渲染）。

GET 只读 + 全简单参数 → 经 G3 自动暴露为 MCP tool。
"""
from typing import Any, Dict

from fastapi import APIRouter

from app.routes._common import err, ok

router = APIRouter()


@router.get("/data/providers")
async def data_providers():
    """数据源能力矩阵 + 可用性（D-J §J.8 / D12）。

    返回默认降级链、运行态实际链（结合依赖/许可证过滤）、各源能力画像、选股可用性
    （screening_ready / screening_providers）。前端据此渲染数据源选择器，并在未连接券商时
    仍能确认「可以选股、用哪个源选」。
    """
    from app.platform import get_platform_status
    status = get_platform_status()
    src = status["sources"]
    return ok({
        "chain_version": src["chain_version"],
        "default_source_policy": src["default_source_policy"],
        "commercial_mode": src["commercial_mode"],
        "capability_chains": src["capability_chains"],
        "chain_resolved": src["chain_resolved"],
        "providers": src["providers"],
        "screening_ready": src["screening_ready"],
        "screening_providers": src["screening_providers"],
        "local_data_available": src["local_data_available"],
    })


@router.get("/data/providers/health")
async def data_providers_health():
    """逐源健康探测（轻量 ping；商用模式跳过禁止商用的源）。"""
    from datasource.providers import provider_catalog
    out = []
    for d in provider_catalog.describe():
        out.append({
            "provider": d["provider"], "name": d["name"],
            "active": d["active"], "dependency_available": d["dependency_available"],
            "commercial_ok": d["commercial_ok"],
            "capabilities": d["capabilities"], "status": d["status"],
        })
    return ok({"providers": out})


@router.post("/data/chain")
async def data_chain_set(body: Dict[str, Any]):
    """运行时覆盖能力降级链（内存态，不持久化；默认契约链不可改，仅覆盖生效）。

    请求：``{chain: {capability: [provider_id,...]}, clear: [capability,...]}``。
    仅接受已知能力 + 已知 provider；未知项立即 400，便于定位（不静默丢弃）。
    解析后的实际链受注册态 / 依赖 / 许可证约束（与默认链同套过滤）。
    """
    from datasource.providers import (
        DEFAULT_CAPABILITY_CHAINS,
        provider_catalog,
    )
    from datasource.registry import get_manager

    chain_req = (body or {}).get("chain") or {}
    clear_req = (body or {}).get("clear") or []
    if not isinstance(chain_req, dict):
        return err(400, "chain 须为 {capability: [provider_id,...]}")
    try:
        for cap, pids in chain_req.items():
            provider_catalog.set_override(cap, pids)
        for cap in clear_req:
            provider_catalog.clear_override(cap)
    except ValueError as exc:
        return err(400, str(exc))

    registered = set(get_manager().list_sources())
    resolved = {
        cap: provider_catalog.resolve_chain(cap, registered=registered)
        for cap in (list(chain_req.keys()) or DEFAULT_CAPABILITY_CHAINS.keys())
    }
    return ok({
        "overrides": {c: list(p) for c, p in provider_catalog._overrides.items()},
        "resolved": resolved,
    })

#: topic 前缀（最长匹配）-> 策略。命名规范 domain:subdomain[:id]
_TOPIC_POLICIES = {
    "market:quote": {
        "ttl_ms": 3000, "min_interval_ms": 1000, "coalesce_within_ms": 500,
        "priority": "high", "stale_ok": True,
    },
    "market:boards": {
        "ttl_ms": 10000, "min_interval_ms": 5000, "coalesce_within_ms": 1000,
        "priority": "medium", "stale_ok": True,
    },
    "market:kline": {
        "ttl_ms": 60000, "min_interval_ms": 10000, "coalesce_within_ms": 2000,
        "priority": "medium", "stale_ok": True,
    },
    "market:etfs": {
        "ttl_ms": 300000, "min_interval_ms": 30000, "coalesce_within_ms": 2000,
        "priority": "low", "stale_ok": True,
    },
    "market:rotation": {
        "ttl_ms": 60000, "min_interval_ms": 10000, "coalesce_within_ms": 2000,
        "priority": "low", "stale_ok": True,
    },
    "market:indicators": {
        "ttl_ms": 300000, "min_interval_ms": 30000, "coalesce_within_ms": 2000,
        "priority": "low", "stale_ok": True,
    },
    "market:indices": {
        "ttl_ms": 15000, "min_interval_ms": 5000, "coalesce_within_ms": 1000,
        "priority": "medium", "stale_ok": True,
    },
    "market:moneyflow": {
        "ttl_ms": 30000, "min_interval_ms": 8000, "coalesce_within_ms": 1000,
        "priority": "medium", "stale_ok": True,
    },
    "market:capital": {
        "ttl_ms": 300000, "min_interval_ms": 30000, "coalesce_within_ms": 2000,
        "priority": "low", "stale_ok": True,
    },
    "market:limitup": {
        "ttl_ms": 10000, "min_interval_ms": 5000, "coalesce_within_ms": 1000,
        "priority": "medium", "stale_ok": True,
    },
    "market:breadth": {
        "ttl_ms": 10000, "min_interval_ms": 5000, "coalesce_within_ms": 1000,
        "priority": "medium", "stale_ok": True,
    },
    "market:sector_stocks": {
        "ttl_ms": 300000, "min_interval_ms": 60000, "coalesce_within_ms": 2000,
        "priority": "low", "stale_ok": True,
    },
    "runtime:jobs": {
        "ttl_ms": 2000, "min_interval_ms": 800, "coalesce_within_ms": 500,
        "priority": "high", "stale_ok": True,
    },
}

_DEFAULT_POLICY = {
    "ttl_ms": 30000, "min_interval_ms": 10000, "coalesce_within_ms": 2000,
    "priority": "medium", "stale_ok": True,
}


@router.get("/datahub/policies")
async def datahub_policies():
    """数据面策略表（单一真源）：{default, topics:{前缀: 策略}}。"""
    return ok({"default": _DEFAULT_POLICY, "topics": _TOPIC_POLICIES})
