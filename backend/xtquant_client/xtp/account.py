"""账户：资金 / 持仓（AccountMixin，自原 xtp.py 逐行搬移）。"""

from ..base import BrokerNotConnectedError
from ._common import _pick



class AccountMixin:


    def get_account(self) -> dict:
        trader, acc = self._require_trader()
        with self._lock:
            a = self._query_asset(trader, acc)
        if a is None:
            raise BrokerNotConnectedError("账户查询返回空（客户端未就绪）")
        return {"account_id": self._account_id, "account_type": self._account_type,
                "cash": self._f(getattr(a, "cash", None)),
                "frozen": self._f(getattr(a, "frozen_cash", None)),
                "market_value": self._f(getattr(a, "market_value", None)),
                "assets": self._f(getattr(a, "total_asset", None))}

    def _query_asset(self, trader, acc):
        """查资产，兼容 ``query_asset``（新 SDK）与 ``query_stock_asset``（旧 SDK）。

        旧版 xttrader 只有 ``query_stock_asset(account)``，无 ``query_asset``；
        直接调 ``query_asset`` 会在该版本 AttributeError。两者返回的资产对象属性
        都是小写（cash/frozen_cash/market_value/total_asset），上层取出一致。
        """
        fn = getattr(trader, "query_asset", None) or getattr(trader, "query_stock_asset", None)
        if fn is None:
            return None
        try:
            return fn(acc)
        except Exception:  # noqa: BLE001
            try:
                return trader.query_stock_asset(acc)
            except Exception:  # noqa: BLE001
                return None

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            pos = trader.query_stock_positions(acc) or []
        out = []
        for p in pos:
            # 多版本属性兼容：旧版全小写，新版部分 CamelCase。
            code = _pick(p, "stock_code", "StockCode")
            if symbol and code != symbol:
                continue
            vol = _pick(p, "volume", "Volume", default=0)
            avail = _pick(p, "can_use_volume", "CanUseVolume", default=0)
            cost = self._f(_pick(p, "open_price", "OpenPrice", "cost_price", "CostPrice"))
            mv = self._f(_pick(p, "market_value", "MarketValue"))
            out.append({"code": code, "name": self._name(code), "volume": vol,
                        "avail": avail, "cost": cost, "market_value": mv})
        return out

    def get_cash(self) -> dict:
        acc = self.get_account()
        return {"cash": acc["cash"], "frozen": acc["frozen"],
                "assets": acc["assets"], "market_value": acc["market_value"]}


__all__ = [
    'AccountMixin',
]
