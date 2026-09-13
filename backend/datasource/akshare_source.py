"""Akshare 数据源（D9/D12 末位兜底）。

定位：链尾保底源。akshare 是「纯 MIT + 全功能 + 仅需网络」的公开源，覆盖面最广
（全市场快照 / 板块 / 指数成分 / 财务 / 资金流）。即便前三位（QMT / eltdx /
baostock）全部不可用，仅凭 akshare 也必须能完成一次全市场选股（D12 底线）。

正因如此，本源必须最健壮：
- ``quote`` 走全市场快照（一次性 HTTP 拿 5000+ 只），带 TTL 本地缓存防东财频控；
- ``kline`` 走 ``stock_zh_a_hist(adjust=qfq/hfq/None)``；
- 每个方法独立 try/except，失败返回 ``None``/``[]``/``{}`` 由上层链路降级，绝不伪造；
- 上游接口易变 → 解析层对「字段缺失」显式报错而非静默降级（Schema 漂移检测）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from typing import ClassVar, Optional

from datasource.base import DataSource

log = __import__("logging").getLogger("qmt_work.datasource.akshare")

# 全市场快照缓存（东财接口有频控，3s 内复用）
_SNAPSHOT_TTL = 3.0
_SNAPSHOT_CACHE: dict = {"ts": 0.0, "data": None}


def available() -> bool:
    return importlib.util.find_spec("akshare") is not None


def _norm_code(code: str) -> str:
    """转 akshare 需要的 ``600519`` 纯数字形态（akshare 多数接口不吃交易所后缀）。"""
    raw = (code or "").strip().upper()
    return raw.split(".")[0] if "." in raw else raw


class AkshareSource(DataSource):
    name = "akshare"
    commercial_ok = True
    capabilities: ClassVar[frozenset[str]] = frozenset({
        "quote", "kline", "kline_qfq", "kline_hfq", "stock_list", "sector",
        "index_constituent", "fundamental", "capital", "suspend", "moneyflow",
        "calendar", "dividend",
    })

    @staticmethod
    def available() -> bool:
        return available()

    # ---------------- 实时行情（全市场快照 + TTL 缓存） ----------------
    async def get_quote(self, code: str) -> dict:
        snap = await self._snapshot()
        row = snap.get(_norm_code(code))
        if not row:
            raise RuntimeError(f"akshare 快照中无该标的行情：{code}")
        return row

    async def _snapshot(self) -> dict:
        now = time.monotonic()
        if _SNAPSHOT_CACHE["data"] is not None and now - _SNAPSHOT_CACHE["ts"] < _SNAPSHOT_TTL:
            return _SNAPSHOT_CACHE["data"]
        data = await asyncio.to_thread(self._fetch_snapshot)
        _SNAPSHOT_CACHE["data"] = data
        _SNAPSHOT_CACHE["ts"] = time.monotonic()
        return data

    @staticmethod
    def _fetch_snapshot() -> dict:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or len(df) == 0:
            raise RuntimeError("akshare 全市场快照返回空")
        out: dict[str, dict] = {}
        for _, r in df.iterrows():
            code = str(r.get("代码", ""))
            if not code:
                continue
            try:
                last = float(r.get("最新价") or 0)
            except (TypeError, ValueError):
                last = 0.0
            out[code] = {
                "code": code,
                "name": str(r.get("名称", "")),
                "last": last,
                "open": float(r.get("今开") or 0),
                "high": float(r.get("最高") or 0),
                "low": float(r.get("最低") or 0),
                "pre_close": float(r.get("昨收") or 0),
                "volume": float(r.get("成交量") or 0) * 100,  # 手 -> 股（统一契约）
                "amount": float(r.get("成交额") or 0),
                "bid": last, "ask": last,
                "pct": float(r.get("涨跌幅") or 0),
                "source": "akshare",
            }
        if not out:
            raise RuntimeError("akshare 全市场快照解析为空（Schema 漂移？）")
        return out

    # ---------------- 历史 K 线 ----------------
    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: Optional[str] = None, start: str = "", end: str = "") -> list:
        if period != "1d":
            raise ValueError("AkshareSource 当前只提供日线")
        return await asyncio.to_thread(self._history, code, count, adjust, start, end)

    @staticmethod
    def _history(code: str, count: int, adjust: Optional[str], start: str, end: str) -> list:
        import akshare as ak
        adj = {"qfq": "qfq", "hfq": "hfq"}.get(adjust or "", "")
        df = ak.stock_zh_a_hist(
            symbol=_norm_code(code), period="daily",
            start_date=start or "19900101", end_date=end or "20500101",
            adjust=adj)
        if df is None or len(df) == 0:
            return []
        rows = []
        for _, r in df.iterrows():
            try:
                rows.append({
                    "time": str(r.get("日期"))[:10],
                    "open": float(r.get("开盘") or 0),
                    "high": float(r.get("最高") or 0),
                    "low": float(r.get("最低") or 0),
                    "close": float(r.get("收盘") or 0),
                    "volume": float(r.get("成交量") or 0) * 100,
                    "amount": float(r.get("成交额") or 0),
                    "source": "akshare",
                })
            except (TypeError, ValueError):
                continue
        return rows[-max(1, min(count, 10000)):]

    # ---------------- 全市场股票列表 ----------------
    async def get_stock_list(self) -> list:
        return await asyncio.to_thread(self._stock_list)

    @staticmethod
    def _stock_list() -> list:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        out = []
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            if not code:
                continue
            out.append({"code": code, "name": str(r.get("name", "")), "category": "A", "status": "1"})
        return out

    # ---------------- 合约详情 ----------------
    async def get_instrument_detail(self, code: str) -> dict:
        return await asyncio.to_thread(self._instrument_detail, code)

    @staticmethod
    def _instrument_detail(code: str) -> dict:
        import akshare as ak
        try:
            info = ak.stock_individual_info_em(symbol=_norm_code(code))
        except Exception as exc:  # noqa: BLE001
            log.warning("akshare instrument_detail 失败：%s", exc)
            return {}
        if info is None or len(info) == 0:
            return {}
        d = dict(zip(info["item"], info["value"]))
        return {
            "code": code,
            "name": str(d.get("股票简称", "")),
            "exchange": str(d.get("交易所", "")),
            "industry": str(d.get("行业", "")),
            "source": "akshare",
        }

    # ---------------- 指数成分 ----------------
    async def get_index_constituents(self, index_code: str) -> list:
        return await asyncio.to_thread(self._index_cons, index_code)

    @staticmethod
    def _index_cons(index_code: str) -> list:
        import akshare as ak
        try:
            df = ak.index_stock_cons_csindex(symbol=index_code)
        except Exception:  # noqa: BLE001
            return []
        if df is None or len(df) == 0:
            return []
        return [{"code": str(r.get("成分券代码", "")), "name": str(r.get("成分券名称", ""))}
                for _, r in df.iterrows()]


__all__ = ["AkshareSource", "available"]
