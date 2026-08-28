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

import csv
import json
import logging
import os
import re
import time

log = logging.getLogger("qmt_work.kline_cache")

_DAILY_PERIODS = ("1d", "1w", "1mon", "1q", "1y", "day", "week", "mon")
_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _safe_name(s) -> str:
    """把 code/period 清洗成合法文件名片段（仅保留字母数字 _ . -）。"""
    return re.sub(r"[^0-9A-Za-z._\-]", "_", str(s)) or "x"


def resample_weekly(bars: list[dict]) -> list[dict]:
    """把日线 K 线聚合为周线（周 一 起 始，ISO 周）。

    聚合规则：open=周首个交易日开盘，high/low=周内最大/最小，close=周最后
    交易日收盘，volume/amount=周内求和，time=该周最后一个交易日。输入须含
    非空 time（YYYY-MM-DD[ ...]）。周线数据均来自同一来源的日线，逐 bar 派生，
    不伪造行情。用于券商原生周线接口（1w）不可用/返回空时的兜底。
    """
    import datetime
    def _d(s):
        """把 bar 的 time 解析为 date；兼容 YYYYMMDD 与 YYYY-MM-DD[ 时间]。"""
        t = str(s or "")[:10].strip()
        if not t:
            return None
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(t, fmt).date()
            except ValueError:
                continue
        return None

    if not bars or _d(bars[0].get("time")) is None:
        return []
    weekly: list[dict] = []
    for raw in sorted(bars, key=lambda b: str(b.get("time") or "")):
        d = _d(raw.get("time"))
        if d is None:
            continue
        t = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
        iso = d.isocalendar()
        cur = weekly[-1] if weekly else None
        if cur is None or (cur["_y"], cur["_w"]) != (iso[0], iso[1]):
            weekly.append({"_y": iso[0], "_w": iso[1], "time": t,
                           "open": raw.get("open"), "high": raw.get("high"),
                           "low": raw.get("low"), "close": raw.get("close"),
                           "volume": raw.get("volume"), "amount": raw.get("amount")})
        else:
            cur["time"] = t
            cur["close"] = raw.get("close", cur["close"])
            if raw.get("high") is not None:
                cur["high"] = raw["high"] if cur["high"] is None else max(cur["high"], raw["high"])
            if raw.get("low") is not None:
                cur["low"] = raw["low"] if cur["low"] is None else min(cur["low"], raw["low"])
            cur["volume"] = (cur.get("volume") or 0) + (raw.get("volume") or 0)
            cur["amount"] = (cur.get("amount") or 0) + (raw.get("amount") or 0)
    for w in weekly:
        w.pop("_y"); w.pop("_w")
    return weekly


def _load_arrow():
    """惰性加载 pyarrow 引擎（feather 写入/读取用）；缺失返回 (None, None)。"""
    try:
        import pyarrow as pa
        import pyarrow.feather as pf
        return pa, pf
    except Exception:  # noqa: BLE001
        return None, None


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

    # ---------------- 阶段 3：异步变体（事件循环内回源/写缓存不阻塞） ----------------
    async def aget(self, code: str, period: str, count: int) -> list[dict]:
        if self.db is None:
            return []
        rows = await self.db.aquery(
            "SELECT dt, open, high, low, close, volume, amount FROM kline_cache "
            "WHERE code=? AND period=? ORDER BY dt DESC LIMIT ?",
            (code, period, max(1, int(count))))
        rows.reverse()
        return [{"time": r["dt"], **{f: r[f] for f in _FIELDS}} for r in rows]

    async def aput(self, code: str, period: str, bars: list[dict]) -> int:
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
                await self.db.aupsert("kline_cache", row)
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("kline cache aput failed %s %s: %s", code, dt, exc)
        return n

    async def alast_fetch(self, code: str, period: str) -> float:
        if self.db is None:
            return 0.0
        row = await self.db.aquery_one(
            "SELECT MAX(fetched_at) AS f, COUNT(1) AS c FROM kline_cache "
            "WHERE code=? AND period=?", (code, period))
        return float((row or {}).get("f") or 0.0)

    async def acount(self, code: str, period: str) -> int:
        if self.db is None:
            return 0
        row = await self.db.aquery_one(
            "SELECT COUNT(1) AS c FROM kline_cache WHERE code=? AND period=?",
            (code, period))
        return int((row or {}).get("c") or 0)

    async def ais_fresh(self, code: str, period: str, count: int) -> bool:
        if await self.acount(code, period) < count:
            return False
        age = time.time() - await self.alast_fetch(code, period)
        return age <= self.ttl_for(period)

    # ---------------- 组合入口 ----------------
    async def get_or_fetch(self, code: str, period: str, count: int, fetcher,
                           force: bool = False) -> dict:
        """缓存优先取 K 线；未命中/过期时回源券商并写缓存。

        fetcher: async (code, period, count) -> list[dict]
        返回 {"bars": [...], "source": cache|broker|cache_stale, "cached_at": float|None}
        """
        count = max(1, int(count))
        # 阶段 3：事件循环内回源/读缓存走 a* 变体（线程池），不阻塞事件循环
        if not force and await self.ais_fresh(code, period, count):
            self.hits += 1
            return {"bars": await self.aget(code, period, count), "source": "cache",
                    "cached_at": await self.alast_fetch(code, period)}
        try:
            bars = await fetcher(code, period, count) or []
            if bars:
                self.misses += 1
                await self.aput(code, period, bars)
                return {"bars": bars, "source": "broker", "cached_at": time.time()}
            # 券商返回空：若有缓存则降级供给
            cached = await self.aget(code, period, count)
            if cached:
                self.stale_serves += 1
                return {"bars": cached, "source": "cache_stale",
                        "cached_at": await self.alast_fetch(code, period),
                        "note": "券商返回空数据，回退到本地历史缓存"}
            self.misses += 1
            return {"bars": [], "source": "broker", "cached_at": None}
        except Exception as exc:  # noqa: BLE001
            cached = await self.aget(code, period, count)
            if cached:
                self.stale_serves += 1
                log.warning("kline fetch failed, serve stale cache %s: %s", code, exc)
                return {"bars": cached, "source": "cache_stale",
                        "cached_at": await self.alast_fetch(code, period),
                        "note": f"券商取数失败（{exc}），回退到本地历史缓存"}
            raise

    # ---------------- 导出到本地指定目录（CSV/JSON，供离线分析/回测归档） ----------------
    def _read_all_bars(self, code: str, period: str, count: int = 0) -> list[dict]:
        """读取某序列全部（或最近 count 根）缓存 K 线，按时间升序。"""
        if self.db is None:
            return []
        count = max(0, int(count or 0))
        if count > 0:
            rows = self.db.query(
                "SELECT dt, open, high, low, close, volume, amount FROM kline_cache "
                "WHERE code=? AND period=? ORDER BY dt DESC LIMIT ?",
                (code, period, count))
            rows.reverse()
        else:
            rows = self.db.query(
                "SELECT dt, open, high, low, close, volume, amount FROM kline_cache "
                "WHERE code=? AND period=? ORDER BY dt", (code, period))
        return [{"time": r["dt"], **{f: r[f] for f in _FIELDS}} for r in rows]

    def all_series(self) -> list[dict]:
        """列出缓存中全部 code×period 序列及行数/最近抓取时间。"""
        if self.db is None:
            return []
        rows = self.db.query(
            "SELECT code, period, COUNT(1) AS rows, MAX(fetched_at) AS last_fetch "
            "FROM kline_cache GROUP BY code, period ORDER BY code, period")
        return [dict(r) for r in rows]

    def export_to(self, code: str, period: str, dest_dir: str,
                  fmt: str = "csv", count: int = 0) -> dict:
        """把某序列历史 K 线导出到 dest_dir/{code}_{period}.{csv|json}。

        纯本地缓存读取，无网络（"快速"路径：先把数据备到缓存再调用本方法）。
        返回 {"code","period","rows","file"}；无数据时 rows=0 且不写文件。
        """
        bars = self._read_all_bars(code, period, count)
        if not bars:
            return {"code": code, "period": period, "rows": 0, "file": ""}
        os.makedirs(dest_dir, exist_ok=True)
        path = self.file_path(code, period, dest_dir, fmt)
        if fmt == "json":
            payload = {"code": code, "period": period, "exported_at": time.time(),
                       "bars": bars}
            with open(path, "w", encoding="utf-8", newline="") as f:
                json.dump(payload, f, ensure_ascii=False)
        elif fmt == "feather":
            return self._export_feather(code, period, bars, path)
        else:
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["time", *_FIELDS])
                w.writeheader()
                for b in bars:
                    w.writerow(b)
        return {"code": code, "period": period, "rows": len(bars), "file": path}

    def _export_feather(self, code: str, period: str, bars: list[dict], path: str) -> dict:
        """用 pyarrow 引擎写 feather（Arrow IPC）。持久化时保留整型时间字段。"""
        pa, pf = _load_arrow()
        if pa is None:
            raise RuntimeError("导出 feather 需要 pyarrow 引擎，请在运行环境安装：pip install pyarrow")
        cols = ["time", *_FIELDS]
        table = pa.Table.from_pydict({c: [b.get(c) for b in bars] for c in cols})
        pf.write_feather(table, path, compression="lz4")
        return {"code": code, "period": period, "rows": len(bars), "file": path}

    @staticmethod
    def file_path(code: str, period: str, dest_dir: str, fmt: str = "csv") -> str:
        """计算导出文件的落盘路径（文件名由 code+period 生成，已做安全清洗）。"""
        ext = {"csv": "csv", "json": "json", "feather": "feather"}.get(fmt, "csv")
        return os.path.join(dest_dir, f"{_safe_name(code)}_{_safe_name(period)}.{ext}")

    @staticmethod
    def read_export(path: str, fmt: str = "csv") -> list[dict]:
        """读取本地导出文件，返回 K 线 bar 列表（离线/断线时也可用）。"""
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        if fmt == "feather":
            pa, pf = _load_arrow()
            if pa is None:
                raise RuntimeError("读取 feather 需要 pyarrow 引擎，请安装：pip install pyarrow")
            return list(pf.read_table(path).to_pylist())
        if fmt == "json":
            with open(path, "r", encoding="utf-8") as f:
                return list(json.load(f).get("bars") or [])
        bars: list[dict] = []
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                b = {"time": row.get("time", "")}
                for k in _FIELDS:
                    v = row.get(k)
                    try:
                        b[k] = None if v in ("", None) else float(v)
                    except (TypeError, ValueError):
                        b[k] = None
                bars.append(b)
        return bars

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
