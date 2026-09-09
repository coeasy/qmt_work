"""Real public Sina/Tencent quote and daily-bar providers."""
from __future__ import annotations

import json
import re

import httpx

from datasource.base import DataSource


def _vendor_code(code: str) -> str:
    bare, _, exchange = code.upper().partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
    if not prefix:
        raise ValueError(f"股票代码必须带交易所后缀: {code}")
    return prefix + bare


class _PublicSource(DataSource):
    capabilities = frozenset({"quote", "kline", "instrument_detail"})

    async def _get(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "qmt_work/1.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_instrument_detail(self, code: str) -> dict:
        quote = await self.get_quote(code)
        return {key: quote.get(key) for key in ("name", "pre_close", "last", "open", "high", "low")}

    async def get_stock_list(self) -> list:
        raise RuntimeError(f"{self.name} 不提供全市场股票列表")


class SinaSource(_PublicSource):
    name = "sina"

    async def get_quote(self, code: str) -> dict:
        vendor = _vendor_code(code)
        raw = await self._get(f"https://hq.sinajs.cn/list={vendor}")
        match = re.search(r'="([^"]*)"', raw)
        if not match or not match.group(1):
            raise RuntimeError(f"新浪未返回行情: {code}")
        fields = match.group(1).split(",")
        if len(fields) < 10:
            raise RuntimeError(f"新浪行情字段不完整: {code}")
        return {"code": code, "name": fields[0], "open": float(fields[1] or 0),
                "pre_close": float(fields[2] or 0), "last": float(fields[3] or 0),
                "high": float(fields[4] or 0), "low": float(fields[5] or 0),
                "volume": float(fields[8] or 0), "amount": float(fields[9] or 0),
                "source": self.name}

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None) -> list:
        if period != "1d":
            raise ValueError("新浪公共源当前只提供日线")
        scale = max(1, min(int(count), 1000))
        raw = await self._get(
            f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={_vendor_code(code)}&scale=240&ma=no&datalen={scale}")
        rows = json.loads(raw)
        return [{"time": r["day"], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r.get("volume", 0)), "source": self.name} for r in rows]


class TencentSource(_PublicSource):
    name = "tencent"

    async def get_quote(self, code: str) -> dict:
        vendor = _vendor_code(code)
        raw = await self._get(f"https://qt.gtimg.cn/q={vendor}")
        match = re.search(r'="([^"]*)"', raw)
        fields = match.group(1).split("~") if match else []
        if len(fields) < 7:
            raise RuntimeError(f"腾讯未返回行情: {code}")
        return {"code": code, "name": fields[1], "last": float(fields[3] or 0),
                "pre_close": float(fields[4] or 0), "open": float(fields[5] or 0),
                "volume": float(fields[6] or 0) * 100, "source": self.name}

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None) -> list:
        if period != "1d":
            raise ValueError("腾讯公共源当前只提供日线")
        adj = adjust if adjust in {"qfq", "hfq"} else "qfq"
        raw = await self._get(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
            f"{_vendor_code(code)},day,,,{max(1, min(int(count), 1000))},{adj}")
        payload = json.loads(raw)
        data = payload.get("data", {}).get(_vendor_code(code), {})
        rows = data.get(adj) or data.get("day") or []
        return [{"time": r[0], "open": float(r[1]), "close": float(r[2]),
                 "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
                 "source": self.name} for r in rows]


__all__ = ["SinaSource", "TencentSource"]
