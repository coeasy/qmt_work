"""Optional real pytdx and BaoStock providers (soft dependencies)."""
from __future__ import annotations

import asyncio
import importlib.util

from datasource.base import DataSource


def _parts(code: str) -> tuple[int, str]:
    bare, _, exchange = code.upper().partition(".")
    if exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError(f"股票代码必须带交易所后缀: {code}")
    return (1 if exchange == "SH" else 0), bare


class TstdxSource(DataSource):
    name = "tstdx"
    capabilities = frozenset({"quote", "kline", "stock_list"})

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("pytdx") is not None

    async def _call(self, operation, *args):
        return await asyncio.to_thread(self._sync_call, operation, *args)

    def _sync_call(self, operation, *args):
        from pytdx.hq import TdxHq_API
        api = TdxHq_API()
        if not api.connect("119.147.212.81", 7709):
            raise RuntimeError("tstdx 无法连接真实行情服务器")
        try:
            return getattr(api, operation)(*args)
        finally:
            api.disconnect()

    async def get_quote(self, code: str) -> dict:
        market, bare = _parts(code)
        rows = await self._call("get_security_quotes", [(market, bare)])
        if not rows:
            raise RuntimeError(f"tstdx 未返回行情: {code}")
        row = rows[0]
        return {"code": code, "last": float(row.get("price", 0)),
                "open": float(row.get("open", 0)), "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)), "pre_close": float(row.get("last_close", 0)),
                "volume": float(row.get("vol", 0)), "amount": float(row.get("amount", 0)),
                "source": self.name}

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None) -> list:
        if period != "1d":
            raise ValueError("tstdx 当前只提供日线")
        market, bare = _parts(code)
        rows = await self._call("get_security_bars", 9, market, bare, 0, max(1, min(count, 800)))
        return [{"time": row["datetime"], "open": float(row["open"]),
                 "high": float(row["high"]), "low": float(row["low"]),
                 "close": float(row["close"]), "volume": float(row["vol"]),
                 "amount": float(row.get("amount", 0)), "source": self.name}
                for row in rows]

    async def get_instrument_detail(self, code: str) -> dict:
        quote = await self.get_quote(code)
        return {"name": quote.get("name", code), "pre_close": quote.get("pre_close")}

    async def get_stock_list(self) -> list:
        rows = []
        for market in (0, 1):
            start = 0
            while True:
                batch = await self._call("get_security_list", market, start)
                rows.extend({"code": f"{'SH' if market else 'SZ'}{r['code']}",
                             "name": r.get("name", ""), "category": "stock"} for r in batch)
                if len(batch) < 1000:
                    break
                start += len(batch)
        return rows


class BaoStockSource(DataSource):
    name = "baostock"
    capabilities = frozenset({"kline", "instrument_detail", "stock_list"})

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("baostock") is not None

    async def get_quote(self, code: str) -> dict:
        raise RuntimeError("BaoStock 不提供实时行情，请显式选择 quote provider")

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None) -> list:
        if period != "1d":
            raise ValueError("BaoStock 当前只提供日线")
        return await asyncio.to_thread(self._history, code, count, adjust)

    @staticmethod
    def _history(code: str, count: int, adjust: str | None) -> list:
        import baostock as bs
        login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock 登录失败: {login.error_msg}")
        try:
            adjustflag = {"qfq": "2", "hfq": "1"}.get(adjust or "", "3")
            rs = bs.query_history_k_data_plus(
                code.lower().replace(".", "."),
                "date,open,high,low,close,volume,amount",
                frequency="d", adjustflag=adjustflag)
            if getattr(rs, "error_code", "0") != "0":
                raise RuntimeError(f"BaoStock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                date, op, high, low, close, volume, amount = rs.get_row_data()
                rows.append({"time": date, "open": float(op), "high": float(high),
                             "low": float(low), "close": float(close),
                             "volume": float(volume or 0), "amount": float(amount or 0),
                             "source": "baostock"})
            return rows[-max(1, min(count, 5000)):]
        finally:
            bs.logout()

    async def get_instrument_detail(self, code: str) -> dict:
        rows = await self.get_stock_list()
        bare = code.upper().split(".")[0]
        return next((row for row in rows if row["code"].endswith(bare)), {})

    async def get_stock_list(self) -> list:
        return await asyncio.to_thread(self._stock_list)

    @staticmethod
    def _stock_list() -> list:
        import baostock as bs
        login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock 登录失败: {login.error_msg}")
        try:
            rs = bs.query_stock_basic()
            rows = []
            while rs.next():
                code, name, ipo, out_date, type_, status = rs.get_row_data()
                rows.append({"code": code.upper(), "name": name, "category": type_, "status": status})
            return rows
        finally:
            bs.logout()


__all__ = ["BaoStockSource", "TstdxSource"]
