"""阶段 1：DB + 持久层 + 基础设施。

- 初始化 SQLite（init_db）
- 券商档案注册表挂接 DB
- API Key 存储绑定 DB
- 通知中心 / 告警引擎 / 运行时配置
- K 线本地缓存（热表）+ 冷仓（热窗口之外的历史，独立 SQLite 文件）
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
    # Durable JobRuntime 在 DB 阶段挂载；若进程曾在任务执行中退出，运行时会
    # 把可恢复任务重新放入队列，并保留 lease/heartbeat/checkpoint 证据。
    from app.runtime.jobs import get_runtime
    # ★ defer_unknown=True：此刻 `system.*` 的 runner 工厂**还没注册**
    #   （`register_all()` 在 phase_misc 才调用）。若不延后判定，崩溃前正在执行的
    #   `system.*` 任务会被误标成「无法恢复未知任务类型」，且此后不再处于
    #   queued/running ⇒ 永不恢复。phase_misc 注册工厂后会再 catch-up 一次。
    get_runtime().attach_db(state.db, defer_unknown=True)

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

    # K 线本地缓存（热表）+ 冷仓（热窗口之外的历史，独立文件）
    #
    # 冷热边界由 runtime_config 的 `market.hot_days` 控制（默认 92 天 ≈ 3 个月）。
    # 冷仓初始化失败**不阻断启动**：`KlineCache(cold=None)` 会把归档访问回退到
    # 主库同名表，行为与改造前一致（宁可退回旧行为，也不要因为一个可选优化
    # 让整个客户端起不来）。
    import asyncio

    from core.config import cold_bars_path
    from datasource.cold_store import init_cold_store
    from gateway.kline_cache import KlineCache
    hot_days = int(state.runtime_config.get("market.hot_days")
                   or settings.bars_hot_days)
    cold = None
    try:
        cold = init_cold_store(cold_bars_path())
        # 一次性搬移：历史版本把冷数据放在**主库**的 kline_archive 表里，
        # 不搬过来的话 `_arch` 指向冷仓后就再也读不到它们 —— 表现为图表历史
        # 静默变短（不报错、不提示，最难查的那种）。幂等：主库表空即空操作。
        moved = await asyncio.to_thread(cold.migrate_from, state.db)
        if moved.get("moved"):
            log.info("冷仓就绪：历史归档搬移 %d 行 → %s",
                     moved["moved"], cold.path)
        else:
            log.info("冷仓就绪：%s（无需搬移）", cold.path)
    except Exception as exc:  # noqa: BLE001
        log.warning("冷仓初始化失败，归档回退主库表（不影响启动）：%s", exc)
        cold = None

    state.kline_cache = KlineCache(
        state.db, ttl_daily=settings.kline_cache_ttl_daily,
        ttl_intraday=settings.kline_cache_ttl_intraday,
        hot_days=hot_days, cold=cold)
    _kc_stats = state.kline_cache.stats()
    log.info("kline cache ready: rows=%s hot=%s cold=%s (hot_days=%s, cold=%s)",
             _kc_stats.get("rows"), _kc_stats.get("hot_rows"),
             _kc_stats.get("archive_rows"), _kc_stats.get("hot_days"),
             _kc_stats.get("cold_enabled"))

    # WAL
    from gateway.wal import WAL
    wal_path = settings.db_path.parent / "wal.jsonl"
    state.wal = WAL(str(wal_path))
    log.info("wal ready: %s", wal_path)

    return {"kline_cache_rows": state.kline_cache.stats().get("rows", 0)}


__all__ = ["setup"]
