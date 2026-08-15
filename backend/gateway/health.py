"""券商连接健康状态机 + 指数退避自动重连。

状态：disconnected -> connecting -> connected | error
- 启动时 / 连接断开后自动进入重连调度
- 指数退避：2^attempt 秒，最大 60s
- 提供 /brokers/{id}/health 查询与 WS 事件推送
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xtquant_client.manager import BrokerManager

log = logging.getLogger("qmt_work")


class BrokerHealthMonitor:
    def __init__(self, manager: BrokerManager, on_event=None, check_interval: float = 5.0):
        self._manager = manager
        self._on_event = on_event
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._stopped = False

    async def start(self):
        await self.stop()
        self._stopped = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass
        self._task = None

    async def _loop(self):
        while not self._stopped:
            try:
                for conn in self._manager.all_connections():
                    await self._check(conn)
            except Exception as exc:  # noqa: BLE001
                log.warning("broker health loop error: %s", exc)
            interval = self._check_interval
            try:
                from app.state import state
                if state.runtime_config is not None:
                    interval = state.runtime_config.health_check_interval
            except Exception:  # noqa: BLE001
                pass
            # 非交易时段放大健康检查间隔（60s 探活）
            from gateway.trading_session import default_session
            await asyncio.sleep(default_session.sleep_seconds(interval, 60.0))

    async def _check(self, conn):
        was_connected = conn.connected
        try:
            ok = conn.adapter.is_connected()
        except Exception as exc:  # noqa: BLE001
            ok = False
            conn.last_error = str(exc)[:200]
        conn.connected = ok
        if ok:
            if conn.health_status != "connected":
                conn.health_status = "connected"
                conn.reconnect_attempts = 0
                if conn.reconnect_task:
                    conn.reconnect_task.cancel()
                    conn.reconnect_task = None
                self._emit(conn.cfg.conn_id, "connected", {"detail": "ok"})
        else:
            if conn.health_status in ("connected", "connecting"):
                conn.health_status = "disconnected"
                self._emit(conn.cfg.conn_id, "disconnected", {"last_error": conn.last_error})
            if conn.cfg.active and conn.reconnect_task is None:
                conn.reconnect_task = asyncio.create_task(self._reconnect(conn))

    async def _reconnect(self, conn):
        conn.health_status = "connecting"
        while not self._stopped and conn.cfg.active:
            delay = min(2 ** conn.reconnect_attempts, 60)
            log.info("reconnect %s in %ss (attempt %d)", conn.cfg.conn_id, delay, conn.reconnect_attempts)
            await asyncio.sleep(delay)
            if self._stopped:
                return
            try:
                await conn.bridge.start()
                if conn.adapter.is_connected():
                    conn.connected = True
                    conn.health_status = "connected"
                    conn.reconnect_attempts = 0
                    conn.last_error = ""
                    self._emit(conn.cfg.conn_id, "connected", {"detail": "reconnected"})
                    return
            except Exception as exc:  # noqa: BLE001
                conn.last_error = str(exc)[:200]
                log.warning("reconnect %s failed: %s", conn.cfg.conn_id, exc)
            conn.reconnect_attempts += 1
            conn.health_status = "error"
        conn.reconnect_task = None

    def _emit(self, conn_id: str, event: str, data: dict):
        if self._on_event:
            try:
                self._on_event({"type": f"broker.{event}", "data": {"conn_id": conn_id, **data}})
            except Exception:  # noqa: BLE001
                pass

    def status(self, conn_id: str) -> dict | None:
        conn = self._manager._conns.get(conn_id)
        if not conn:
            return None
        return {
            "conn_id": conn_id,
            "status": conn.health_status,
            "connected": conn.connected,
            "active": conn.cfg.active,
            "reconnect_attempts": conn.reconnect_attempts,
            "last_error": conn.last_error,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
