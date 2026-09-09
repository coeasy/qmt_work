"""Configurable HTTP trading connector for a real broker gateway."""
from __future__ import annotations

from typing import Any

import httpx

from .ports import ConnectorDescriptor, ConnectorError, OrderRequest


class HttpTradingConnector:
    """Second connector implementation; endpoint and credentials are explicit.

    This is an adapter for a real broker-side HTTP gateway, not an in-process
    simulator. It performs a health check before accepting order commands.
    """

    def __init__(self, base_url: str, api_key: str, *, connector_id: str = "http-trading"):
        if not base_url or not api_key:
            raise ValueError("HTTP trading connector requires base_url and api_key")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url,
                                         headers={"Authorization": f"Bearer {api_key}"},
                                         timeout=10.0)
        self.descriptor = ConnectorDescriptor(
            id=connector_id, name="HTTP Trading Gateway", version="v1",
            capabilities=("trade", "account", "positions"), optional_sdk="",
        )
        self._connected = False

    async def start(self) -> None:
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
            self._connected = True
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            raise ConnectorError(f"HTTP trading gateway unavailable: {exc}") from exc

    async def close(self) -> None:
        self._connected = False
        await self._client.aclose()

    def is_connected(self) -> bool:
        return self._connected

    async def place_order(self, request: OrderRequest) -> dict[str, Any]:
        request.validate()
        if not self._connected:
            raise ConnectorError("HTTP trading gateway is not connected")
        response = await self._client.post("/orders", json={
            "symbol": request.instrument.canonical, "side": request.side,
            "order_type": request.order_type, "price": request.price,
            "quantity": request.quantity, "account_id": request.account_id,
            "client_order_id": request.client_order_id,
            "strategy_name": request.strategy_name, "remark": request.remark,
        })
        response.raise_for_status()
        return response.json()

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        if not self._connected:
            raise ConnectorError("HTTP trading gateway is not connected")
        response = await self._client.delete(f"/orders/{order_id}")
        response.raise_for_status()
        return response.json()

