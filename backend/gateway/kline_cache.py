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

_DAILY_PERIODS = ("1d", "1w", "1mon", "1q", "1y", "day", "week", "mon", "month")
_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _rows_from(rows: list[dict]) -> list[dict]:
    """把双表查询出的原始行统一转为 bar 字典（含 time 与 adjust）。"""
    return [{"time": r["dt"], **{f: r[f] for f in _FIELDS},
             "adjust": r.get("adjust") or ""} for r in rows]


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

    # ---------------- 热/归档路由（核心架构：热表=今年，归档表=今年以前） ----------------
    _HOT = "kline_cache"
    _ARCHIVE = "kline_archive"

    @staticmethod
    def _year_start() -> str:
        """今年 1 月 1 日字符串（作为动态分区界）。"""
        return f"{time.localtime().tm_year}-01-01"

    @staticmethod
    def _year_of(dt) -> int:
        """稳健提取 dt 的年份：兼容 YYYY-MM-DD[ 时间] 与 YYYYMMDD 两种格式。
        取前 4 位数字，解析失败按 0 处理（避免路由错表）。"""
        s = str(dt or "")
        digits = "".join(ch for ch in s if ch.isdigit())[:4]
        try:
            return int(digits)
        except ValueError:
            return 0

    @staticmethod
    def _is_hot(dt: str) -> bool:
        """dt 是否属于今年（写入热表）：仅按年份比较，不依赖日期字符串格式。"""
        return KlineCache._year_of(dt) >= time.localtime().tm_year

    def _where(self, table: str, code: str, period: str, adjust: str = "") -> str:
        # M6：复权维度入唯一键后，读路径必须按 adjust 过滤，
        # 否则 qfq/hfq/原始价三份数据混排（同一 dt 多根 K 线）。
        return (f"SELECT dt, open, high, low, close, volume, amount, adjust FROM {table} "
                f"WHERE code=? AND period=? AND adjust=? ORDER BY dt DESC")

    def get(self, code: str, period: str, count: int, adjust: str = "") -> list[dict]:
        """取最近 count 根（今年热表优先 + 历史归档补足），按时间升序返回。

        adjust: ''=券商原始价 / qfq / hfq —— 只返回该复权维度的数据。
        """
        if self.db is None:
            return []
        count = max(1, int(count))
        adj = adjust or ""
        hot = self.db.query(self._where(self._HOT, code, period, adj) + " LIMIT ?",
                            (code, period, adj, count))
        need = count - len(hot)
        arch: list[dict] = []
        if need > 0:
            arch = self.db.query(self._where(self._ARCHIVE, code, period, adj) + " LIMIT ?",
                                 (code, period, adj, need))
        rows = _rows_from(arch + hot)
        rows.sort(key=lambda x: str(x["time"] or ""))
        return rows

    def put(self, code: str, period: str, bars: list[dict], adjust: str = "") -> int:
        """写入/刷新 K 线（按年份路由热/归档表，单事务批量 upsert）。

        去年以前的历史落归档表，今年数据落热表（由同步任务定时更新）。
        返回写入热表的条数。
        """
        if self.db is None or not bars:
            return 0
        hot_rows: list[tuple] = []
        arch_rows: list[tuple] = []
        now = time.time()
        for b in bars:
            dt = str(b.get("time") or b.get("dt") or "").strip()
            if not dt:
                continue
            vals = [None] * 6
            for i, f in enumerate(_FIELDS):
                v = b.get(f)
                try:
                    vals[i] = None if v is None else float(v)
                except (TypeError, ValueError):
                    vals[i] = None
            row = (code, period, dt, *vals, now, adjust or "")
            (hot_rows if self._is_hot(dt) else arch_rows).append(row)
        cols = ("code,period,dt," + ",".join(_FIELDS) + ",fetched_at,adjust")
        if hot_rows:
            self.db.executemany_in_txn(
                f"INSERT OR REPLACE INTO {self._HOT} ({cols}) VALUES ({','.join('?' * 11)})",
                hot_rows)
        if arch_rows:
            self.db.executemany_in_txn(
                f"INSERT OR REPLACE INTO {self._ARCHIVE} ({cols}) VALUES ({','.join('?' * 11)})",
                arch_rows)
        return len(hot_rows)

    def last_fetch(self, code: str, period: str, adjust: str = "") -> float:
        if self.db is None:
            return 0.0
        adj = adjust or ""
        row = self.db.query_one(
            f"SELECT MAX(fetched_at) AS f FROM {self._HOT} "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adj))
        a = self.db.query_one(
            f"SELECT MAX(fetched_at) AS f FROM {self._ARCHIVE} "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adj))
        return max(float((row or {}).get("f") or 0.0),
                   float((a or {}).get("f") or 0.0))

    def last_adjust(self, code: str, period: str) -> str:
        """读取序列最近一根的复权标记（qfq/hfq/''），用于抓取刷新时复用。"""
        if self.db is None:
            return ""
        for table in (self._HOT, self._ARCHIVE):
            row = self.db.query_one(
                f"SELECT adjust FROM {table} WHERE code=? AND period=? "
                "AND adjust!='' ORDER BY dt DESC LIMIT 1", (code, period))
            if row and row.get("adjust"):
                return row["adjust"]
        return ""

    def count(self, code: str, period: str) -> int:
        if self.db is None:
            return 0
        row = self.db.query_one(
            f"SELECT COUNT(1) AS c FROM {self._HOT} WHERE code=? AND period=?",
            (code, period))
        a = self.db.query_one(
            f"SELECT COUNT(1) AS c FROM {self._ARCHIVE} WHERE code=? AND period=?",
            (code, period))
        return int((row or {}).get("c") or 0) + int((a or {}).get("c") or 0)

    def is_fresh(self, code: str, period: str, count: int) -> bool:
        """缓存足量且未过期。"""
        if self.count(code, period) < count:
            return False
        age = time.time() - self.last_fetch(code, period)
        return age <= self.ttl_for(period)

    # ---------------- 阶段 3：异步变体（事件循环内回源/写缓存不阻塞） ----------------
    async def aget(self, code: str, period: str, count: int, adjust: str = "") -> list[dict]:
        if self.db is None:
            return []
        count = max(1, int(count))
        adj = adjust or ""
        hot = await self.db.aquery(self._where(self._HOT, code, period, adj) + " LIMIT ?",
                                   (code, period, adj, count))
        need = count - len(hot)
        arch: list[dict] = []
        if need > 0:
            arch = await self.db.aquery(self._where(self._ARCHIVE, code, period, adj) + " LIMIT ?",
                                        (code, period, adj, need))
        rows = _rows_from(arch + hot)
        rows.sort(key=lambda x: str(x["time"] or ""))
        return rows

    async def aput(self, code: str, period: str, bars: list[dict], adjust: str = "") -> int:
        if self.db is None or not bars:
            return 0
        hot_rows: list[tuple] = []
        arch_rows: list[tuple] = []
        now = time.time()
        for b in bars:
            dt = str(b.get("time") or b.get("dt") or "").strip()
            if not dt:
                continue
            vals = [None] * 6
            for i, f in enumerate(_FIELDS):
                v = b.get(f)
                try:
                    vals[i] = None if v is None else float(v)
                except (TypeError, ValueError):
                    vals[i] = None
            row = (code, period, dt, *vals, now, adjust or "")
            (hot_rows if self._is_hot(dt) else arch_rows).append(row)
        cols = ("code,period,dt," + ",".join(_FIELDS) + ",fetched_at,adjust")
        if hot_rows:
            await self.db.aexecutemany_in_txn(
                f"INSERT OR REPLACE INTO {self._HOT} ({cols}) VALUES ({','.join('?' * 11)})",
                hot_rows)
        if arch_rows:
            await self.db.aexecutemany_in_txn(
                f"INSERT OR REPLACE INTO {self._ARCHIVE} ({cols}) VALUES ({','.join('?' * 11)})",
                arch_rows)
        return len(hot_rows)

    async def alast_fetch(self, code: str, period: str, adjust: str = "") -> float:
        if self.db is None:
            return 0.0
        adj = adjust or ""
        row = await self.db.aquery_one(
            f"SELECT MAX(fetched_at) AS f FROM {self._HOT} WHERE code=? AND period=? AND adjust=?",
            (code, period, adj))
        a = await self.db.aquery_one(
            f"SELECT MAX(fetched_at) AS f FROM {self._ARCHIVE} WHERE code=? AND period=? AND adjust=?",
            (code, period, adj))
        return max(float((row or {}).get("f") or 0.0), float((a or {}).get("f") or 0.0))

    async def alast_adjust(self, code: str, period: str) -> str:
        if self.db is None:
            return ""
        for table in (self._HOT, self._ARCHIVE):
            row = await self.db.aquery_one(
                f"SELECT adjust FROM {table} WHERE code=? AND period=? "
                "AND adjust!='' ORDER BY dt DESC LIMIT 1", (code, period))
            if row and row.get("adjust"):
                return row["adjust"]
        return ""

    async def acount(self, code: str, period: str, adjust: str = "") -> int:
        if self.db is None:
            return 0
        adj = adjust or ""
        row = await self.db.aquery_one(
            f"SELECT COUNT(1) AS c FROM {self._HOT} WHERE code=? AND period=? AND adjust=?",
            (code, period, adj))
        a = await self.db.aquery_one(
            f"SELECT COUNT(1) AS c FROM {self._ARCHIVE} WHERE code=? AND period=? AND adjust=?",
            (code, period, adj))
        return int((row or {}).get("c") or 0) + int((a or {}).get("c") or 0)

    async def ais_fresh(self, code: str, period: str, count: int, adjust: str = "") -> bool:
        if await self.acount(code, period, adjust) < count:
            return False
        age = time.time() - await self.alast_fetch(code, period, adjust)
        return age <= self.ttl_for(period)

    # ---------------- 组合入口 ----------------
    async def get_or_fetch(self, code: str, period: str, count: int, fetcher,
                           force: bool = False, adjust: str = "") -> dict:
        """缓存优先取 K 线；未命中/过期时回源券商并写缓存。

        fetcher: async (code, period, count) -> list[dict]
        adjust: 本请求的复权维度（''=原始价 / qfq / hfq）。缓存读写均按该维度
        隔离（M6：唯一键含 adjust 后三份数据各行其道，读路径不再混排）。
        返回 {"bars": [...], "source": cache|broker|cache_stale, "cached_at": float|None}
        """
        count = max(1, int(count))
        adj = adjust or ""
        # 阶段 3：事件循环内回源/读缓存走 a* 变体（线程池），不阻塞事件循环
        if not force and await self.ais_fresh(code, period, count, adj):
            self.hits += 1
            return {"bars": await self.aget(code, period, count, adj), "source": "cache",
                    "cached_at": await self.alast_fetch(code, period, adj)}
        try:
            bars = await fetcher(code, period, count) or []
            if bars:
                self.misses += 1
                # M6 修正：按本请求的复权维度落库（券商原始价=''）。
                # 旧逻辑复用 alast_adjust 会把原始价错标成 qfq/hfq；
                # 唯一键含 adjust 后各维度独立存储，无需再互相"保护"。
                await self.aput(code, period, bars, adjust=adj)
                return {"bars": bars, "source": "broker", "cached_at": time.time()}
            # 券商返回空：若有缓存则降级供给
            cached = await self.aget(code, period, count, adj)
            if cached:
                self.stale_serves += 1
                return {"bars": cached, "source": "cache_stale",
                        "cached_at": await self.alast_fetch(code, period, adj),
                        "note": "券商返回空数据，回退到本地历史缓存"}
            self.misses += 1
            return {"bars": [], "source": "broker", "cached_at": None}
        except Exception as exc:  # noqa: BLE001
            cached = await self.aget(code, period, count, adj)
            if cached:
                self.stale_serves += 1
                log.warning("kline fetch failed, serve stale cache %s: %s", code, exc)
                return {"bars": cached, "source": "cache_stale",
                        "cached_at": await self.alast_fetch(code, period, adj),
                        "note": f"券商取数失败（{exc}），回退到本地历史缓存"}
            raise

    # ---------------- 导出到本地指定目录（CSV/JSON，供离线分析/回测归档） ----------------
    def _read_all_bars(self, code: str, period: str, count: int = 0) -> list[dict]:
        """读取某序列全部（或最近 count 根）K 线（热表+归档合并），按时间升序。

        导出场景读全部复权维度（M6 唯一键含 adjust），按 (adjust, dt) 去重防御。
        """
        if self.db is None:
            return []
        count = max(0, int(count or 0))
        if count > 0:
            raw = self.get(code, period, count)
        else:
            rows = self.db.query(
                f"SELECT dt, open, high, low, close, volume, amount, adjust "
                f"FROM {self._HOT} WHERE code=? AND period=? ORDER BY dt DESC",
                (code, period))
            rows += self.db.query(
                f"SELECT dt, open, high, low, close, volume, amount, adjust "
                f"FROM {self._ARCHIVE} WHERE code=? AND period=? ORDER BY dt DESC",
                (code, period))
            raw = _rows_from(rows)
            # 同一 (adjust, dt) 唯一；跨维度去重只看 (time, adjust)
            seen: set = set()
            dedup = []
            for b in sorted(raw, key=lambda x: (str(x.get("adjust") or ""),
                                                str(x["time"] or ""))):
                t = (str(b.get("time") or ""), str(b.get("adjust") or ""))
                if t in seen:
                    continue
                seen.add(t)
                dedup.append(b)
            raw = dedup
        return raw

    def all_series(self) -> list[dict]:
        """列出缓存中全部 code×period 序列及行数/最近抓取时间（含热表与归档）。"""
        if self.db is None:
            return []
        rows = self.db.query(
            f"SELECT code, period, COUNT(1) AS rows, MAX(fetched_at) AS last_fetch "
            f"FROM {self._HOT} GROUP BY code, period "
            f"UNION ALL SELECT code, period, COUNT(1) AS rows, MAX(fetched_at) AS last_fetch "
            f"FROM {self._ARCHIVE} GROUP BY code, period "
            f"ORDER BY code, period")
        merged: dict = {}
        for r in rows:
            k = (r["code"], r["period"])
            if k not in merged:
                merged[k] = {"code": r["code"], "period": r["period"],
                             "rows": 0, "last_fetch": r["last_fetch"]}
            m = merged[k]
            m["rows"] += int(r.get("rows") or 0)
            if r.get("last_fetch") and m["last_fetch"]:
                m["last_fetch"] = max(m["last_fetch"], r["last_fetch"])
            elif r.get("last_fetch"):
                m["last_fetch"] = r["last_fetch"]
        return list(merged.values())

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
            cols = ["time", *_FIELDS, "adjust"]
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for b in bars:
                    w.writerow(b)
        return {"code": code, "period": period, "rows": len(bars), "file": path}

    def _export_feather(self, code: str, period: str, bars: list[dict], path: str) -> dict:
        """用 pyarrow 引擎写 feather（Arrow IPC）。持久化时保留整型时间字段。"""
        pa, pf = _load_arrow()
        if pa is None:
            raise RuntimeError("导出 feather 需要 pyarrow 引擎，请在运行环境安装：pip install pyarrow")
        cols = ["time", *_FIELDS, "adjust"]
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
                b.setdefault("adjust", row.get("adjust") or "")
                bars.append(b)
        return bars

    # ---------------- 年度归档维护 ----------------
    def archive_rollover(self) -> dict:
        """把热表中早于今年的行搬入归档（跨年维护，幂等）。

        年份判断按 Python 侧稳健提取的年份，避免 YYYYMMDD / YYYY-MM-DD 混排
        导致的字符串比较错位；用 id 精确删除，不误删今年数据。
        返回 {"moved": N, "deleted": N}。
        """
        if self.db is None:
            return {"moved": 0, "deleted": 0}
        now_year = time.localtime().tm_year
        rows = self.db.query(
            f"SELECT id, code, period, dt, open, high, low, close, volume, amount, "
            f"fetched_at, adjust FROM {self._HOT}")
        to_move = [r for r in rows if self._year_of(r["dt"]) < now_year]
        if not to_move:
            return {"moved": 0, "deleted": 0}
        cols = ("code,period,dt," + ",".join(_FIELDS) + ",fetched_at,adjust")
        self.db.executemany_in_txn(
            f"INSERT OR REPLACE INTO {self._ARCHIVE} ({cols}) VALUES ({','.join('?' * 11)})",
            [(r["code"], r["period"], r["dt"], r["open"], r["high"], r["low"],
              r["close"], r["volume"], r["amount"], r["fetched_at"], r["adjust"])
             for r in to_move])
        self.db.executemany_in_txn(
            f"DELETE FROM {self._HOT} WHERE id=?",
            [(r["id"],) for r in to_move])
        return {"moved": len(to_move), "deleted": len(to_move)}

    # ---------------- 运维 ----------------
    def stats(self) -> dict:
        total = 0
        symbols = 0
        hot = 0
        archive_rows = 0
        if self.db is not None:
            row = self.db.query_one(
                f"SELECT COUNT(1) AS c, COUNT(DISTINCT code||'|'||period) AS s FROM {self._HOT}")
            total = int((row or {}).get("c") or 0)
            symbols = int((row or {}).get("s") or 0)
            hot = total
            a = self.db.query_one(
                f"SELECT COUNT(1) AS c, COUNT(DISTINCT code||'|'||period) AS s "
                f"FROM {self._ARCHIVE}")
            archive_rows = int((a or {}).get("c") or 0)
            symbols += int((a or {}).get("s") or 0)
            total += archive_rows
        served = self.hits + self.misses + self.stale_serves
        return {"rows": total, "hot_rows": hot, "archive_rows": archive_rows,
                "series": symbols, "current_year": self._year_start(),
                "hits": self.hits, "misses": self.misses,
                "stale_serves": self.stale_serves,
                "hit_rate": round(self.hits / served, 4) if served else None,
                "ttl_daily": self.ttl_daily, "ttl_intraday": self.ttl_intraday}

    def clear(self, code: str = "", period: str = "") -> int:
        """清空整表（或按 code/period）。热表与归档一并清理。"""
        if self.db is None:
            return 0
        n = 0
        for table in (self._HOT, self._ARCHIVE):
            if code and period:
                cur = self.db.execute(f"DELETE FROM {table} WHERE code=? AND period=?",
                                      (code, period))
            elif code:
                cur = self.db.execute(f"DELETE FROM {table} WHERE code=?", (code,))
            else:
                cur = self.db.execute(f"DELETE FROM {table}")
            n += int(cur.rowcount or 0)
        return n
