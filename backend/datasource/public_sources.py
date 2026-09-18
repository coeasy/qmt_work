"""Real public Sina/Tencent quote and daily-bar providers."""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from datasource.base import DataSource

log = __import__("logging").getLogger("qmt_work.datasource.public_sources")


def _vendor_code(code: str) -> str:
    bare, _, exchange = code.upper().partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
    if not prefix:
        raise ValueError(f"股票代码必须带交易所后缀: {code}")
    return prefix + bare


class _PublicSource(DataSource):
    capabilities = frozenset({"quote", "kline", "instrument_detail"})

    #: 单源**最小请求间隔**（秒）—— 免费公开接口有速率限制，全市场批量同步
    #: （5000+ 只）若不节流会被直接封禁（实测腾讯返回 ``501 Not Implemented``，
    #: 且**不限 count**：连 20 根的请求也一并 501）。届时降级链路整体失效，
    #: 5000 只又静默退回券商的陈旧数据 —— 正是 §8.8 要消灭的「假成功」。
    #: 0.3s ⇒ 约 3 req/s，全市场约 30 分钟，是被封与吞吐之间的折中。
    _MIN_INTERVAL = 0.3
    _last_call = 0.0
    _gate = None

    @classmethod
    async def _throttle(cls) -> None:
        """串行化本源的请求并保证最小间隔（同类共享，跨实例生效）。

        锁**懒创建**：模块导入时不一定有事件循环，直接建 asyncio.Lock 会在
        某些 Python 版本绑到错误的循环上。
        """
        if cls._gate is None:
            cls._gate = asyncio.Lock()
        async with cls._gate:
            now = time.monotonic()
            wait = cls._MIN_INTERVAL - (now - cls._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_call = time.monotonic()

    async def _get(self, url: str) -> str:
        await self._throttle()
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "qmt_work/1.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_instrument_detail(self, code: str) -> dict:
        quote = await self.get_quote(code)
        return {key: quote.get(key) for key in ("name", "pre_close", "last", "open", "high", "low")}

    async def get_stock_list(self) -> list:
        # 公共源不提供全市场列表 —— 这里 raise 是**有意的显式拒绝**，不是待实现桩。
        # 对应的能力声明（见 capabilities）也不含 "stock_list"，故 resolve_chain 的
        # 能力校验不会把公共源排进 stock_list 链（V11 R6：此前注册序候选链会真的
        # 调到这里并计入熔断失败）。
        raise RuntimeError(f"{self.name} 不提供全市场股票列表")


class SinaSource(_PublicSource):
    name = "sina"
    # 新浪 get_kline 忽略 adjust 参数（只回不复权日线），故**不声明**复权变体能力；
    # 契约链 kline_qfq/kline_hfq 也确实未列入 sina（两处一致）。

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
    # V11 R6：腾讯 get_kline 真的处理 adjust（映射到 fqkline 的 adj 参数），
    # 契约链也把 tencent 列在 kline_qfq/kline_hfq 里 —— 此前声明漏了这两个变体，
    # 属「链里有、声明无」，会让能力校验误剔（或反向让链形同虚设）。
    capabilities = frozenset({"quote", "kline", "kline_qfq", "kline_hfq",
                              "instrument_detail"})

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
        # ★ 复权键名是 ``"<adj>day"`` 而不是 ``"<adj>"``（V11 R13 实测修正）。
        # 接口实际返回的键：qfq → ``qfqday``、hfq → ``hfqday``、不复权 → ``day``。
        # 此前按 ``data.get(adj) or data.get("day")`` 取，对**有复权历史**的标的
        # 恒取不到（键根本不叫 ``qfq``），只有返回裸 ``day`` 的（无除权的次新股、
        # 科创板新股）才侥幸成功 —— 实测全市场 5153 只里**只有 49 只**能取到，
        # 其余 5093 只腾讯源恒返空。
        #
        # 后果很严重：券商本地历史陈旧（实测只到 20250418）时本该降级到在线源，
        # 而在线源此刻恒返空 ⇒ 「降级」永远拿不到数据，5093 只股票的日线
        # 一年多没更新，同步却照报成功。
        rows = data.get(f"{adj}day") or data.get(adj) or data.get("day") or []
        if not rows:
            # 键名形状将来再变时不静默返空：写日志，避免又是一次「查不到原因的空」
            log.warning("腾讯 K 线无数据：%s（adj=%s，可用键=%s）",
                        code, adj, ",".join(sorted(data.keys())) or "无")
        return [{"time": r[0], "open": float(r[1]), "close": float(r[2]),
                 "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
                 "source": self.name} for r in rows]


__all__ = ["SinaSource", "TencentSource"]
