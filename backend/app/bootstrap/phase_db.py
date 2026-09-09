"""阶段 1：DB + 持久层 + 基础设施。

- 初始化 SQLite（init_db）
- 券商档案注册表挂接 DB
- API Key 存储绑定 DB
- 通知中心 / 告警引擎 / 运行时配置
- K 线本地缓存
- WAL
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from core.config import settings
from core.db import init_db
from core.state import state

log = logging.getLogger("qmt_work.bootstrap.db")


async def setup(app: FastAPI) -> dict:
    state.db = init_db(settings.db_path)

    # 券商档案注册表挂接 DB（热插拔档案落库 + 加载已持久化档案）
    from xtquant_client.registry import registry as broker_registry
    broker_registry.attach_db(state.db)
    state.mcp = getattr(app.state, "mcp", None) or state.mcp

    # 多密钥存储绑定 DB
    if state.apikey_store is not None:
        state.apikey_store.bind(state.db)
        state.apikey_store.reload()

    # 通知中心
    from gateway.notifier import Notifier
    state.notifier = Notifier(state.db, dedup_seconds=settings.notify_dedup_seconds)
    log.info("notifier ready")

    # 告警引擎
    from gateway.alert_engine import AlertEngine
    state.alert_engine = AlertEngine(state.db, state.notifier)
    state.notifier.on_event = state.alert_engine.evaluate_event
    log.info("alert engine ready")

    # 运行时配置
    from gateway.runtime_config import RuntimeConfig
    state.runtime_config = RuntimeConfig(state.db)
    log.info("runtime config ready: %d keys", len(state.runtime_config.all()))

    # K 线本地缓存
    from gateway.kline_cache import KlineCache
    state.kline_cache = KlineCache(
        state.db, ttl_daily=settings.kline_cache_ttl_daily,
        ttl_intraday=settings.kline_cache_ttl_intraday)
    log.info("kline cache ready: %s", state.kline_cache.stats().get("rows"))

    # WAL
    from gateway.wal import WAL
    wal_path = settings.db_path.parent / "wal.jsonl"
    state.wal = WAL(str(wal_path))
    log.info("wal ready: %s", wal_path)

    return {"kline_cache_rows": state.kline_cache.stats().get("rows", 0)}


__all__ = ["setup"]
