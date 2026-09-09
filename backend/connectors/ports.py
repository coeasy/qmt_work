"""Canonical connector contracts.

The existing ``xtquant_client`` package remains the compatibility implementation.
These contracts are deliberately small: application services depend on canonical
commands and snapshots, while a connector owns SDK-specific mapping and lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class ConnectorError(RuntimeError):
    """A connector could not perform a real operation."""


class ConnectorState(str, Enum):
    DISCONNECTED = "disconnected"
    STARTING = "starting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class ConnectorDescriptor:
    id: str
    name: str
    version: str
    capabilities: tuple[str, ...] = ()
    optional_sdk: str = ""
    account_types: tuple[str, ...] = ("STOCK",)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InstrumentId:
    code: str
    exchange: str = ""

    @property
    def canonical(self) -> str:
        return self.code if not self.exchange else f"{self.code}.{self.exchange}"


@dataclass(frozen=True)
class OrderRequest:
    instrument: InstrumentId
    side: str
    order_type: str = "limit"
    price: float = 0.0
    quantity: int = 0
    account_id: str = ""
    client_order_id: str = ""
    strategy_name: str = ""
    remark: str = ""

    def validate(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"unsupported order side: {self.side}")
        if self.order_type not in {"limit", "market"}:
            raise ValueError(f"unsupported order type: {self.order_type}")
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.order_type == "limit" and self.price <= 0:
            raise ValueError("limit order price must be positive")


@dataclass(frozen=True)
class PositionSnapshot:
    instrument: InstrumentId
    quantity: int
    available_quantity: int = 0
    average_cost: float = 0.0
    market_value: float = 0.0


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    cash: float
    frozen: float = 0.0
    assets: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class ConnectorPort(Protocol):
    """Async boundary used by lifecycle and execution orchestration."""

    descriptor: ConnectorDescriptor

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def is_connected(self) -> bool: ...

    async def place_order(self, request: OrderRequest) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...
