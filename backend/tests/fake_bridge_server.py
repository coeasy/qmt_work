"""桥接子进程的测试替身服务端（不依赖真实 xtquant）。

复用生产 `xtquant_client.bridge_server.serve_adapter` 协议实现，
用一个内存 MockAdapter 响应 RPC，用于验证完整 IPC 传输（请求/响应/事件/关闭）。

由 BridgeAdapter 测试以 `python -m tests.fake_bridge_server` 拉起。
"""

from xtquant_client.base import BrokerNotConnectedError
from xtquant_client.bridge_server import serve_adapter


class MockAdapter:
    def __init__(self):
        self._connected = False

    def start(self):
        self._connected = True

    def close(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def _simulate_disconnect(self):
        """测试辅助：模拟 SDK 断开（仅翻转标志；状态泵会轮询到并推送 conn_state:false）。"""
        self._connected = False

    def _simulate_disconnect_error(self):
        """测试辅助：模拟查询时发现 SDK 已断开（翻转标志 + 抛 BrokerNotConnectedError）。"""
        self._connected = False
        raise BrokerNotConnectedError("SDK 已断开（模拟）")

    def get_quote(self, code):
        return {"code": code, "last": 10.0, "open": 9.0}

    def get_full_tick(self, codes):
        return {c: {"code": c, "last": 10.0} for c in codes}

    def get_kline(self, code, period, count, start="", end=""):
        return [{"time": "2026-01-01", "open": 10, "high": 11,
                 "low": 9, "close": 10.5, "volume": 1000}]

    def get_tick(self, code):
        return self.get_quote(code)

    def get_stock_list(self, sector="沪深A股"):
        return [{"code": "600519.SH", "name": "贵州茅台"}]

    def search_stocks(self, keyword, limit=20):
        return [{"code": "600519.SH", "name": "贵州茅台"}]

    def subscribe_quote(self, codes, on_tick):
        if on_tick:
            on_tick({"type": "quote",
                     "data": {"code": codes[0] if codes else "", "last": 11.0,
                              "ts": "2026-01-01T10:00:00"}})

    def get_account(self):
        return {"account_id": "MOCK", "assets": 100.0, "cash": 50.0,
                "market_value": 50.0}

    def get_positions(self, symbol=None):
        return [{"code": "600519.SH", "name": "贵州茅台", "volume": 100,
                 "market_value": 50.0}]

    def get_cash(self):
        return {"cash": 50.0, "frozen": 0.0, "assets": 100.0, "market_value": 50.0}

    def get_orders(self):
        return [{"order_id": "O1", "code": "600519.SH", "direction": "buy",
                 "status": "submitted"}]

    def get_deals(self):
        return [{"order_id": "O1", "code": "600519.SH", "direction": "buy",
                 "price": 10.0, "volume": 100, "time": "2026-01-01"}]

    def place_order(self, code, direction, price_type, price, volume,
                    strategy_name="", remark=""):
        return {"order_id": "MOCK1", "code": code, "direction": direction,
                "price_type": price_type, "price": price, "volume": volume,
                "status": "submitted"}

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "cancel_submitted"}

    def get_sector_list(self):
        return ["沪深A股"]

    def get_sector_stocks(self, sector="沪深A股"):
        return ["600519.SH"]

    def get_trading_calendar(self, start="", end=""):
        return ["20260101", "20260102"]

    def get_financial(self, code):
        return {"code": code, "EPS": 1.0}

    def get_l2_transactions(self, code, count=100):
        return [{"price": 10.0, "volume": 100, "type": "buy"}]


if __name__ == "__main__":
    raise SystemExit(serve_adapter(MockAdapter()))
