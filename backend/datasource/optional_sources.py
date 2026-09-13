"""Optional BaoStock provider (soft dependency)."""
from __future__ import annotations

import asyncio
import importlib.util

from datasource.base import DataSource


def _to_baostock_code(code: str) -> str:
    """将任意 A 股代码形态规整为 baostock 要求的 ``sh.600519`` / ``sz.000001`` 形态。

    baostock 的 ``query_history_k_data_plus`` 只接受 ``交易所前缀.数字`` 形态，
    旧代码 ``code.lower().replace(".", ".")`` 是空操作（把 ``600519.SH`` 原样传
    入），导致查询恒失败、该源实际不可用（P0-16 根因）。
    """
    raw = (code or "").strip().upper()
    if raw.startswith(("SH.", "SZ.")):   # 已是 baostock 形态
        return raw.lower()
    if "." in raw:
        sym, exch = raw.split(".", 1)
        exch = exch.lower()
    else:
        sym, exch = raw, ""
    if not exch:
        # 无交易所前缀时按代码首位推断：6/9/5 开头为上交所，其余为深交所。
        exch = "sh" if sym[:1] in ("6", "9", "5") else "sz"
    return f"{exch}.{sym}".lower()


class BaoStockSource(DataSource):
    name = "baostock"
    capabilities = frozenset({"kline", "instrument_detail", "stock_list",
                              "suspend", "fundamental", "index_constituent", "dividend"})
    commercial_ok = True

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("baostock") is not None

    async def get_quote(self, code: str) -> dict:
        raise RuntimeError("BaoStock 不提供实时行情，请显式选择 quote provider")

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None, start: str = "", end: str = "") -> list:
        if period != "1d":
            raise ValueError("BaoStock 当前只提供日线")
        return await asyncio.to_thread(self._history, code, count, adjust, start, end)

    @staticmethod
    def _history(code: str, count: int, adjust: str | None,
                 start: str = "", end: str = "") -> list:
        import baostock as bs
        login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock 登录失败: {login.error_msg}")
        try:
            adjustflag = {"qfq": "2", "hfq": "1"}.get(adjust or "", "3")
            rs = bs.query_history_k_data_plus(
                _to_baostock_code(code),
                "date,open,high,low,close,volume,amount",
                frequency="d", adjustflag=adjustflag,
                start_date=start or "", end_date=end or "")
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


__all__ = ["BaoStockSource"]
