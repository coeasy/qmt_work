"""Canonical connector contracts.

The existing ``xtquant_client`` package remains the compatibility implementation.
These contracts are deliberately small: application services depend on canonical
commands and snapshots, while a connector owns SDK-specific mapping and lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


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


@runtime_checkable
class ConnectorPort(Protocol):
    """Async boundary used by lifecycle and execution orchestration."""

    descriptor: ConnectorDescriptor

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def is_connected(self) -> bool: ...

    async def place_order(self, request: OrderRequest) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# V9 Phase 6（D-A）：9 端口拆分 —— 应用服务依赖窄接口，连接器负责 SDK 映射。
# 全部 runtime_checkable，供 test_connector_ports.py 做结构化契约测试。
# ---------------------------------------------------------------------------

@runtime_checkable
class ExecutionPort(Protocol):
    """交易执行：下单 / 撤单 / 委托与成交查询。"""

    async def place_order(self, request: OrderRequest) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    def get_orders(self) -> list[dict[str, Any]]: ...

    def get_deals(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class AccountPort(Protocol):
    """账户资产：净值摘要与资金。"""

    def get_account(self) -> dict[str, Any]: ...

    def get_cash(self) -> dict[str, Any]: ...


@runtime_checkable
class PositionsPort(Protocol):
    """持仓快照查询。"""

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class MarketDataPort(Protocol):
    """历史/快照行情：K 线、报价、全 tick。"""

    def get_quote(self, code: str) -> dict[str, Any]: ...

    def get_kline(self, code: str, period: str, count: int,
                  adjust: str = "") -> list[dict[str, Any]]: ...

    def get_full_tick(self, codes: list[str]) -> dict[str, Any]: ...


@runtime_checkable
class QuoteFeedPort(Protocol):
    """实时行情订阅（回调式）。"""

    def subscribe_quote(self, codes: list[str],
                        on_tick: Callable[[dict], None]) -> None: ...


@runtime_checkable
class InstrumentPort(Protocol):
    """合约与板块基础数据。"""

    def get_instrument_detail(self, code: str) -> dict[str, Any]: ...

    def get_stock_list(self, sector: str = "沪深A股") -> list[dict[str, Any]]: ...

    def get_sector_list(self) -> list[str]: ...


@runtime_checkable
class CalendarPort(Protocol):
    """交易日历。"""

    def get_trading_calendar(self, start: str = "",
                             end: str = "") -> list[str]: ...


@runtime_checkable
class HealthPort(Protocol):
    """连接健康：探活 + 结构化诊断。"""

    def is_connected(self) -> bool: ...

    def test_connection(self) -> dict[str, Any]: ...


@runtime_checkable
class CapabilityPort(Protocol):
    """能力协商：连接器声明自己支持什么（quote/kline/trade/...）。"""

    descriptor: ConnectorDescriptor

    def capabilities(self) -> tuple[str, ...]: ...


#: 9 端口清单（DoD 测试与文档引用的唯一真源）
CANONICAL_PORTS: tuple[str, ...] = (
    "ConnectorPort", "ExecutionPort", "AccountPort", "PositionsPort",
    "MarketDataPort", "QuoteFeedPort", "InstrumentPort", "CalendarPort",
    "HealthPort", "CapabilityPort",
)
