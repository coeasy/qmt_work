"""QMT/XTQuant implementation of the canonical connector port."""
from __future__ import annotations

from typing import Any

from xtquant_client.base import BrokerAdapter, BrokerError
from xtquant_client.gateway import XTQuantBridge

from .ports import ConnectorDescriptor, ConnectorError, ConnectorPort, OrderRequest


class QmtConnector:
    """Adapter around the real XTQuant adapter and its thread-safe bridge.

    No SDK calls are made until ``start``.  This keeps QMT optional for research,
    backtest and paper workflows while preserving the existing bridge semantics.
    """

    def __init__(self, adapter: BrokerAdapter):
        self.adapter = adapter
        self.bridge = XTQuantBridge(adapter)
        self.descriptor = ConnectorDescriptor(
            id="qmt",
            name="QMT / XTQuant",
            version=getattr(adapter, "client_version", "") or "unknown",
            capabilities=("quote", "kline", "trade", "account", "positions", "realtime"),
            optional_sdk=getattr(adapter, "sdk_required", "xtquant") or "xtquant",
            account_types=tuple(getattr(adapter, "supported_account_types", ["STOCK"])),
        )

    async def start(self) -> None:
        try:
            await self.bridge.start()
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"QMT connector start failed: {exc}") from exc

    async def close(self) -> None:
        await self.bridge.stop()

    def is_connected(self) -> bool:
        try:
            return bool(self.adapter.is_connected())
        except Exception:  # noqa: BLE001
            return False

    async def place_order(self, request: OrderRequest) -> dict[str, Any]:
        request.validate()
        try:
            return await self.bridge.call_locked(
                self.adapter.place_order,
                request.instrument.canonical,
                request.side,
                request.order_type,
                request.price,
                request.quantity,
                request.strategy_name,
                request.remark,
            )
        except (BrokerError, OSError) as exc:
            raise ConnectorError(str(exc)) from exc

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        try:
            return await self.bridge.call_locked(self.adapter.cancel_order, order_id)
        except (BrokerError, OSError) as exc:
            raise ConnectorError(str(exc)) from exc

    async def quote(self, code: str) -> dict[str, Any]:
        if not code:
            raise ValueError("code is required")
        try:
            return await self.bridge.call(self.adapter.get_quote, code)
        except (BrokerError, OSError) as exc:
            raise ConnectorError(str(exc)) from exc
