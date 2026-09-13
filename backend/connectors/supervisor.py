"""Multi-connection lifecycle supervisor independent of a single active broker."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .ports import ConnectorPort, ConnectorState

log = logging.getLogger("qmt_work.connectors.supervisor")


@dataclass
class ConnectionStatus:
    connector_id: str
    state: ConnectorState = ConnectorState.DISCONNECTED
    last_error: str = ""
    reconnect_attempts: int = 0


class ConnectionSupervisor:
    """Owns independent connector lifecycles; one failed connector cannot stop others.

    V9 Phase 8：内置健康探测 + 指数退避自动重连循环（可选启用）。
    - ``watch(interval)`` 启动后台守护任务：周期探测非 STOPPING/DISCONNECTED 连接，
      掉线即自动重连（退避上限 60s，成功后归零）；
    - 用户显式 ``stop`` 的连接不会被自动拉起（尊重人工意图）；
    - 单连接故障绝不影响其他连接（与既有隔离语义一致）。
    """

    def __init__(self, max_backoff: float = 60.0):
        self._connectors: dict[str, ConnectorPort] = {}
        self._status: dict[str, ConnectionStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watch_task: asyncio.Task | None = None
        self._max_backoff = float(max_backoff)
        self._backoff: dict[str, float] = {}

    def register(self, connector_id: str, connector: ConnectorPort) -> None:
        if not connector_id or connector_id in self._connectors:
            raise ValueError(f"connector id already registered: {connector_id}")
        self._connectors[connector_id] = connector
        self._status[connector_id] = ConnectionStatus(connector_id)
        self._locks[connector_id] = asyncio.Lock()

    def status(self, connector_id: str) -> ConnectionStatus:
        try:
            return self._status[connector_id]
        except KeyError as exc:
            raise KeyError(f"unknown connector: {connector_id}") from exc

    def statuses(self) -> list[ConnectionStatus]:
        return list(self._status.values())

    async def start(self, connector_id: str) -> ConnectionStatus:
        connector = self._get(connector_id)
        async with self._locks[connector_id]:
            status = self._status[connector_id]
            if status.state == ConnectorState.CONNECTED and connector.is_connected():
                return status
            status.state = ConnectorState.STARTING
            try:
                await connector.start()
                status.state = (ConnectorState.CONNECTED if connector.is_connected()
                                else ConnectorState.DEGRADED)
                status.last_error = ""
            except Exception as exc:  # noqa: BLE001
                status.state = ConnectorState.FAILED
                status.last_error = str(exc)[:500]
                status.reconnect_attempts += 1
                log.warning("connector %s start failed: %s", connector_id, exc)
            return status

    async def stop(self, connector_id: str) -> ConnectionStatus:
        connector = self._get(connector_id)
        async with self._locks[connector_id]:
            status = self._status[connector_id]
            status.state = ConnectorState.STOPPING
            try:
                await connector.close()
            finally:
                status.state = ConnectorState.DISCONNECTED
            # 显式人工停机：清零退避，标记不自动拉起（state 停在 DISCONNECTED）
            self._backoff.pop(connector_id, None)
            return status

    async def start_all(self) -> list[ConnectionStatus]:
        return list(await asyncio.gather(*(self.start(cid) for cid in self._connectors)))

    async def stop_all(self) -> list[ConnectionStatus]:
        return list(await asyncio.gather(*(self.stop(cid) for cid in self._connectors)))

    # ------------------------------------------------------------------
    # V9 Phase 8：健康探测 + 自动重连
    # ------------------------------------------------------------------
    def watch(self, interval: float = 30.0) -> asyncio.Task:
        """启动健康守护循环（幂等）：周期探测，掉线自动重连（指数退避）。"""
        if self._watch_task is not None and not self._watch_task.done():
            return self._watch_task

        async def _loop() -> None:
            log.info("connection supervisor watch started (interval=%ss)", interval)
            while True:
                try:
                    await asyncio.sleep(max(5.0, float(interval)))
                    await self._health_check()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("supervisor watch tick failed: %s", exc)

        self._watch_task = asyncio.create_task(_loop())
        return self._watch_task

    def unwatch(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None

    async def _health_check(self) -> None:
        for cid, connector in list(self._connectors.items()):
            status = self._status.get(cid)
            if status is None or status.state in (ConnectorState.STOPPING,
                                                   ConnectorState.DISCONNECTED):
                continue  # 人工停机/停机中的连接不自动拉起
            healthy = False
            try:
                healthy = bool(connector.is_connected())
            except Exception:  # noqa: BLE001
                healthy = False
            if healthy:
                if status.state != ConnectorState.CONNECTED:
                    status.state = ConnectorState.CONNECTED
                    status.last_error = ""
                self._backoff.pop(cid, None)
                continue
            # 掉线：指数退避自动重连
            delay = self._backoff.get(cid, 1.0)
            if status.state == ConnectorState.CONNECTED:
                log.warning("connector %s lost connection, auto reconnect in %.0fs",
                            cid, delay)
            status.state = ConnectorState.DEGRADED
            await asyncio.sleep(min(delay, self._max_backoff))
            try:
                await self.start(cid)
            except Exception as exc:  # noqa: BLE001
                log.warning("connector %s auto reconnect failed: %s", cid, exc)
            self._backoff[cid] = min(delay * 2.0, self._max_backoff)

    def _get(self, connector_id: str) -> ConnectorPort:
        try:
            return self._connectors[connector_id]
        except KeyError as exc:
            raise KeyError(f"unknown connector: {connector_id}") from exc


__all__ = ["ConnectionStatus", "ConnectionSupervisor"]
