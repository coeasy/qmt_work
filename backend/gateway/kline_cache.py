"""C1 历史 K 线本地缓存（SQLite kline_cache 表）。

价值：
- 回测/图表反复取同一段历史 K 线时不再穿透到券商客户端（xtdata 单次调用数百毫秒起）；
- 券商未连接或临时断线时，可以用**此前真实抓取过的**历史数据继续跑回测/看图，
  响应中以 `source=cache_stale` + `cached_at` 明确标注来源，绝不伪造行情。

新鲜度策略：
- 日线（1d/1w/1mon）：当天已抓过即视为新鲜（`ttl_daily`，默认 6h）；
- 分钟线及更细粒度：短 TTL（`ttl_intraday`，默认 60s）。
历史 bar 本身不可变，只有"最后一根"会变化，因此 TTL 只用于决定是否回源刷新。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("qmt_work.kline_cache")

_DAILY_PERIODS = ("1d", "1w", "1mon", "1q", "1y", "day", "week", "mon")
_FIELDS = ("open", "high", "low", "close", "volume", "amount")


class KlineCache:
    def __init__(self, db, ttl_daily: float = 6 * 3600.0, ttl_intraday: float = 60.0):
        self.db = db
        self.ttl_daily = ttl_daily
        self.ttl_intraday = ttl_intraday
        self.hits = 0
        self.misses = 0
        self.stale_serves = 0

    # ---------------- 基础读写 ----------------
    def ttl_for(self, period: str) -> float:
        return self.ttl_daily if str(period).lower() in _DAILY_PERIODS else self.ttl_intraday

    def get(self, code: str, period: str, count: int) -> list[dict]:
        """取最近 count 根缓存 K 线（按时间升序返回）。"""
        if self.db is None:
            return []
        rows = self.db.query(
            "SELECT dt, open, high, low, close, volume, amount FROM kline_cache "
            "WHERE code=? AND period=? ORDER BY dt DESC LIMIT ?",
            (code, period, max(1, int(count))))
        rows.reverse()
        return [{"time": r["dt"], **{f: r[f] for f in _FIELDS}} for r in rows]

    def put(self, code: str, period: str, bars: list[dict]) -> int:
        """写入/刷新缓存（按 code+period+dt 幂等 upsert）。"""
        if self.db is None or not bars:
            return 0
        now = time.time()
        n = 0
        for b in bars:
            dt = str(b.get("time") or b.get("dt") or "").strip()
            if not dt:
                continue
            row = {"code": code, "period": period, "dt": dt, "fetched_at": now}
            for f in _FIELDS:
                v = b.get(f)
                try:
                    row[f] = None if v is None else float(v)
                except (TypeError, ValueError):
                    row[f] = None
            try:
                self.db.upsert("kline_cache", row)
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("kline cache put failed %s %s: %s", code, dt, exc)
        return n

    def last_fetch(self, code: str, period: str) -> float:
        if self.db is None:
            return 0.0
        row = self.db.query_one(
            "SELECT MAX(fetched_at) AS f, COUNT(1) AS c FROM kline_cache "
            "WHERE code=? AND period=?", (code, period))
        return float((row or {}).get("f") or 0.0)

    def count(self, code: str, period: str) -> int:
        if self.db is None:
            return 0
        row = self.db.query_one(
            "SELECT COUNT(1) AS c FROM kline_cache WHERE code=? AND period=?",
            (code, period))
        return int((row or {}).get("c") or 0)

    def is_fresh(self, code: str, period: str, count: int) -> bool:
        """缓存足量且未过期。"""
        if self.count(code, period) < count:
            return False
        age = time.time() - self.last_fetch(code, period)
        return age <= self.ttl_for(period)

    # ---------------- 组合入口 ----------------
    async def get_or_fetch(self, code: str, period: str, count: int, fetcher,
                           force: bool = False) -> dict:
        """缓存优先取 K 线；未命中/过期时回源券商并写缓存。

        fetcher: async (code, period, count) -> list[dict]
        返回 {"bars": [...], "source": cache|broker|cache_stale, "cached_at": float|None}
        """
        count = max(1, int(count))
        if not force and self.is_fresh(code, period, count):
            self.hits += 1
            return {"bars": self.get(code, period, count), "source": "cache",
                    "cached_at": self.last_fetch(code, period)}
        try:
            bars = await fetcher(code, period, count) or []
            if bars:
                self.misses += 1
                self.put(code, period, bars)
                return {"bars": bars, "source": "broker", "cached_at": time.time()}
            # 券商返回空：若有缓存则降级供给
            cached = self.get(code, period, count)
            if cached:
                self.stale_serves += 1
                return {"bars": cached, "source": "cache_stale",
                        "cached_at": self.last_fetch(code, period),
                        "note": "券商返回空数据，回退到本地历史缓存"}
            self.misses += 1
            return {"bars": [], "source": "broker", "cached_at": None}
        except Exception as exc:  # noqa: BLE001
            cached = self.get(code, period, count)
            if cached:
                self.stale_serves += 1
                log.warning("kline fetch failed, serve stale cache %s: %s", code, exc)
                return {"bars": cached, "source": "cache_stale",
                        "cached_at": self.last_fetch(code, period),
                        "note": f"券商取数失败（{exc}），回退到本地历史缓存"}
            raise

    # ---------------- 运维 ----------------
    def stats(self) -> dict:
        total = 0
        symbols = 0
        if self.db is not None:
            row = self.db.query_one(
                "SELECT COUNT(1) AS c, COUNT(DISTINCT code||'|'||period) AS s FROM kline_cache")
            total = int((row or {}).get("c") or 0)
            symbols = int((row or {}).get("s") or 0)
        served = self.hits + self.misses + self.stale_serves
        return {"rows": total, "series": symbols, "hits": self.hits,
                "misses": self.misses, "stale_serves": self.stale_serves,
                "hit_rate": round(self.hits / served, 4) if served else None,
                "ttl_daily": self.ttl_daily, "ttl_intraday": self.ttl_intraday}

    def clear(self, code: str = "", period: str = "") -> int:
        if self.db is None:
            return 0
        if code and period:
            cur = self.db.execute("DELETE FROM kline_cache WHERE code=? AND period=?",
                                  (code, period))
        elif code:
            cur = self.db.execute("DELETE FROM kline_cache WHERE code=?", (code,))
        else:
            cur = self.db.execute("DELETE FROM kline_cache")
        return int(cur.rowcount or 0)
