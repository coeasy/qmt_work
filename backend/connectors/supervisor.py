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
    """Owns independent connector lifecycles; one failed connector cannot stop others."""

    def __init__(self):
        self._connectors: dict[str, ConnectorPort] = {}
        self._status: dict[str, ConnectionStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
            return status

    async def start_all(self) -> list[ConnectionStatus]:
        return list(await asyncio.gather(*(self.start(cid) for cid in self._connectors)))

    async def stop_all(self) -> list[ConnectionStatus]:
        return list(await asyncio.gather(*(self.stop(cid) for cid in self._connectors)))

    def _get(self, connector_id: str) -> ConnectorPort:
        try:
            return self._connectors[connector_id]
        except KeyError as exc:
            raise KeyError(f"unknown connector: {connector_id}") from exc


__all__ = ["ConnectionStatus", "ConnectionSupervisor"]
