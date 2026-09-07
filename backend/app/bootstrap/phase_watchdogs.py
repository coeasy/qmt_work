"""阶段 4：监控/守护/算法引擎。

- DB 备份（启动时一次 + 后台周期）
- 系统状态广播
- 券商健康监控 + 自动重连
- 行情泵守护（2s 周期补注册）
- 涨停监控
- 算法单引擎
- 条件单引擎
- 订单超时守护
- 统一信号入口
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.state import state
from fastapi import FastAPI

log = logging.getLogger("qmt_work.bootstrap.watchdogs")


async def setup(app: FastAPI) -> dict:
    # DB 备份
    db_backup = None
    if settings.db_backup_enabled:
        from gateway.db_backup import DBBackup
        db_backup = DBBackup(
            settings.db_path, keep=settings.db_backup_keep,
            interval=settings.db_backup_interval, db=state.db)
        db_backup.backup_once("startup")
        await db_backup.start()
        log.info("db backup enabled: interval=%.0fs keep=%d",
                 settings.db_backup_interval, settings.db_backup_keep)
    # 把 db_backup 暴露给停机阶段
    app.state._db_backup = db_backup

    # 系统状态广播
    async def _system_broadcast_loop():
        import time
        from gateway.trading_session import default_session
        from app.version import __version__
        while True:
            await asyncio.sleep(5.0)
            if state.ws_manager is None:
                continue
            try:
                payload = {
                    "uptime_seconds": int(time.time() - state.started_at) if state.started_at else 0,
                    "version": __version__,
                    "brokers_total": len(state.broker_manager.all_connections()),
                    "brokers_connected": sum(1 for c in state.broker_manager.all_connections() if c.connected),
                    "clients": state.ws_manager.client_count(),
                    "trading_session": default_session.stats(),
                    "started_at": state.started_at,
                }
                await state.ws_manager.broadcast("system", {"event": "tick", **payload})
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("system broadcast failed: %s", exc)
    _system_task = asyncio.create_task(_system_broadcast_loop())
    app.state._system_task = _system_task

    # 健康监控
    from gateway.health import BrokerHealthMonitor
    state.health_monitor = BrokerHealthMonitor(
        state.broker_manager, state.ws_manager.broadcast, check_interval=5.0)
    await state.health_monitor.start()

    # 行情泵守护
    async def _pump_guard():
        while True:
            await asyncio.sleep(2.0)
            loop = asyncio.get_running_loop()
            for conn in state.broker_manager.all_connections():
                b = conn.bridge
                if b is None or not (conn.cfg.active or conn.connected):
                    continue
                if state.sync_engine is not None:
                    try:
                        b.ensure_handler("quote", state.sync_engine.on_event)
                        state.sync_engine.register_realtime_trade_handlers(conn)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("handler guard %s: %s", conn.cfg.conn_id, exc)
                if not b.pump_running():
                    try:
                        b.start_pump_on(loop)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("pump guard %s: %s", conn.cfg.conn_id, exc)
    _pump_task = asyncio.create_task(_pump_guard())
    app.state._pump_task = _pump_task

    # 涨停监控 + 算法单引擎 + 条件单引擎 + 订单守护
    from tools.algo import AlgoEngine
    from tools.condition_order import ConditionOrderEngine
    from tools.limitup import LimitUpMonitor
    state.limitup_monitor = LimitUpMonitor(state.broker_manager, state.risk,
                                           state.ws_manager.broadcast, wal=state.wal)
    state.algo_engine = AlgoEngine(state.broker_manager, state.risk,
                                   state.ws_manager.broadcast, wal=state.wal,
                                   notifier=state.notifier)
    state.condition_engine = ConditionOrderEngine(
        state.broker_manager, state.risk, state.db, state.ws_manager.broadcast,
        wal=state.wal, notifier=state.notifier)
    state.condition_engine.load_from_db()
    await state.condition_engine.start(interval=2.0)

    from gateway.order_watchdog import OrderWatchdog
    state.order_watchdog = OrderWatchdog(
        state.broker_manager, timeout=settings.order_watchdog_timeout,
        interval=settings.order_watchdog_interval,
        enabled=settings.order_watchdog_enabled,
        on_event=state.ws_manager.broadcast, notifier=state.notifier)
    await state.order_watchdog.start()

    # 统一信号入口
    from gateway.signal_router import SignalRouter
    state.signal_router = SignalRouter(
        state.broker_manager, state.risk, state.db, state.wal,
        state.notifier, state.ws_manager.broadcast, runtime_config=state.runtime_config)

    return {}


__all__ = ["setup"]
