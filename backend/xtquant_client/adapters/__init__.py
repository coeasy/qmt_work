"""其他券商/客户端家族适配器（接口契约）。

这些适配器实现统一 `BrokerAdapter` 接口，但真实调用依赖对应券商 SDK：
- 同花顺（ths）：需安装同花顺量化 SDK
- 恒生 PTrade（ptrade）：需安装 PTrade / iFinD 客户端与 SDK
- 掘金（juejin）：需安装掘金量化 `gm` 包

在未安装对应 SDK 的机器上，方法抛 `BrokerSDKError`（明确安装指引），绝不返回假数据。
安装 SDK 并实现各方法体内的真实调用即可启用该券商。
"""
from ..base import BrokerAdapter, BrokerSDKError


class ExternalBrokerAdapter(BrokerAdapter):
    """外部券商适配器的通用基类：方法体待接入真实 SDK，当前统一抛 SDK 缺失错误。"""

    broker_name = "external"
    adapter_id = "external"
    client_version = ""
    sdk_required = ""

    @property
    def supported_periods(self) -> list[str]:
        return ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"]

    @property
    def supported_account_types(self) -> list[str]:
        return ["STOCK"]

    def start(self) -> None:
        raise BrokerSDKError(self.sdk_required, self._install_hint())

    def close(self) -> None:
        pass

    def is_connected(self) -> bool:
        return False

    def _install_hint(self) -> str:
        return f"请安装 {self.sdk_required} 并在本适配器内实现真实调用。"

    def _not_impl(self, *a, **k):
        raise BrokerSDKError(self.sdk_required, self._install_hint())

    def get_quote(self, code: str) -> dict: return self._not_impl(code)
    def get_full_tick(self, codes: list[str]) -> dict: return self._not_impl(codes)
    def get_kline(self, code: str, period: str, count: int, start: str = "", end: str = "") -> list[dict]:
        return self._not_impl(code, period, count)
    def get_tick(self, code: str) -> dict: return self._not_impl(code)
    def get_stock_list(self, sector: str = "沪深A股") -> list[dict]: return self._not_impl(sector)
    def subscribe_quote(self, codes: list[str], on_tick) -> None: self._not_impl(codes, on_tick)
    def get_account(self) -> dict: return self._not_impl()
    def get_positions(self, symbol: str | None = None) -> list[dict]: return self._not_impl(symbol)
    def get_cash(self) -> dict: return self._not_impl()
    def get_orders(self) -> list[dict]: return self._not_impl()
    def get_deals(self) -> list[dict]: return self._not_impl()
    def place_order(self, code: str, direction: str, price_type: str, price: float,
                    volume: int, strategy_name: str = "", remark: str = "") -> dict:
        return self._not_impl(code, direction, price_type, price, volume)
    def cancel_order(self, order_id: str) -> dict: return self._not_impl(order_id)
