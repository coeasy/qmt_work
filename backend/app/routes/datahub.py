"""G4 统一数据面：TopicPolicy 策略表（后端下发，杜绝前后端漂移）。

前端数据总线（lib/dataHub.js）以本策略表为**单一真源**：ttl_ms（快照缓存寿命）/
min_interval_ms（同 topic 最小请求间隔）/ coalesce_within_ms（合并窗口，多个订阅
在窗口内合并为一次网络请求）/ priority / stale_ok（陈旧快照可先行渲染）。

GET 只读 + 全简单参数 → 经 G3 自动暴露为 MCP tool。
"""
from fastapi import APIRouter

from app.routes._common import ok

router = APIRouter()

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
