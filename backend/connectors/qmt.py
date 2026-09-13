"""QMT/XTQuant implementation of the canonical connector port."""
from __future__ import annotations

from typing import Any

from xtquant_client.base import BrokerAdapter, BrokerError
from xtquant_client.gateway import XTQuantBridge

from .ports import ConnectorDescriptor, ConnectorError, OrderRequest


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

    # ------------------------------------------------------------------
    # V9 Phase 6：端口面补全（ExecutionPort 之外的 8 个窄接口的 QMT 映射）。
    # 全部委托真实 adapter（经 bridge.call_locked 串行化 SDK 访问），零 mock。
    # ------------------------------------------------------------------
    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        try:
            return await self.bridge.call_locked(self.adapter.cancel_order, order_id)
        except (BrokerError, OSError) as exc:
            raise ConnectorError(str(exc)) from exc

    def get_orders(self) -> list[dict[str, Any]]:
        return list(self.adapter.get_orders() or [])

    def get_deals(self) -> list[dict[str, Any]]:
        return list(self.adapter.get_deals() or [])

    def get_account(self) -> dict[str, Any]:
        return dict(self.adapter.get_account() or {})

    def get_cash(self) -> dict[str, Any]:
        return dict(self.adapter.get_cash() or {})

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return list(self.adapter.get_positions(symbol) or [])

    def get_quote(self, code: str) -> dict[str, Any]:
        return dict(self.adapter.get_quote(code) or {})

    def get_kline(self, code: str, period: str, count: int,
                  adjust: str = "") -> list[dict[str, Any]]:
        # 注：adapter 层无 adjust 形参（复权由数据面 sync/bars 承担），此处仅透传基础参数
        return list(self.adapter.get_kline(code, period, count) or [])

    def get_full_tick(self, codes: list[str]) -> dict[str, Any]:
        return dict(self.adapter.get_full_tick(codes) or {})

    def subscribe_quote(self, codes: list[str], on_tick) -> None:
        self.adapter.subscribe_quote(codes, on_tick)

    def get_instrument_detail(self, code: str) -> dict[str, Any]:
        return dict(self.adapter.get_instrument_detail(code) or {})

    def get_stock_list(self, sector: str = "沪深A股") -> list[dict[str, Any]]:
        return list(self.adapter.get_stock_list(sector) or [])

    def get_sector_list(self) -> list[str]:
        return list(self.adapter.get_sector_list() or [])

    def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        return list(self.adapter.get_trading_calendar(start, end) or [])

    def test_connection(self) -> dict[str, Any]:
        return dict(self.adapter.test_connection() or {})

    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.descriptor.capabilities)


__all__ = ["QmtConnector"]
