"""C1 历史 K 线本地缓存（SQLite kline_cache 表）。

价值：
- 回测/图表反复取同一段历史 K 线时不再穿透到券商客户端（xtdata 单次调用数百毫秒起）；
- 券商未连接或临时断线时，可以用**此前真实抓取过的**历史数据继续跑回测/看图，
  响应中以 `source=cache_stale` + `cached_at` 明确标注来源，绝不伪造行情。

新鲜度策略：
- 日线（1d/1w/1mon）：当天已抓过即视为新鲜（`ttl_daily`，默认 6h）；
- 分钟线及更细粒度：短 TTL（`ttl_intraday`，默认 60s）。
历史 bar 本身不可变，只有"最后一根"会变化，因此 TTL 只用于决定是否回源刷新。

冷热分层（2026-09-17）：
- **热表** ``kline_cache``（主库）只留最近 ``hot_days`` 天（默认 92 天 ≈ 3 个月）；
- **冷仓** ``kline_archive`` 放在**独立 SQLite 文件**（``datasource/cold_store.py``），
  早于热窗口的行搬过去，此后每日定时同步**只刷新热窗口内的数据**；
- 读路径 ``get()`` / ``aget()`` 对冷热**透明**：先取热表，不足部分从冷仓补足，
  消费方（图表/回测/选股）无需知道数据分了几层。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import timedelta

from core.clock import local_now, to_iso

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
    def __init__(self, db, ttl_daily: float = 6 * 3600.0, ttl_intraday: float = 60.0,
                 hot_days: int = 92, cold=None):
        """``cold`` 是冷仓（:class:`datasource.cold_store.ColdStore`）或 ``None``。

        ``cold=None`` 时归档访问**回退主库**的同名表 —— 老库、单元测试、以及
        「冷仓文件还没建起来」的启动早期都走这条路，行为与改造前完全一致。
        """
        self.db = db
        self.ttl_daily = ttl_daily
        self.ttl_intraday = ttl_intraday
        self.hot_days = max(1, int(hot_days))
        self._cold = cold
        self.hits = 0
        self.misses = 0
        self.stale_serves = 0

    # ---------------- 基础读写 ----------------
    def ttl_for(self, period: str) -> float:
        return self.ttl_daily if str(period).lower() in _DAILY_PERIODS else self.ttl_intraday

    # ---------------- 热/归档路由（核心架构：热表=最近 hot_days 天，冷仓=更早） ----------------
    _HOT = "kline_cache"
    _ARCHIVE = "kline_archive"

    @property
    def _arch(self):
        """归档表的宿主：冷仓可用时是冷仓，否则回退主库（老库/测试环境）。

        所有归档读写都必须经这里 —— 直接写 ``self.db`` 会把冷数据又写回主库，
        热窗口就白拆了。
        """
        return self._cold if self._cold is not None else self.db

    @property
    def cold_enabled(self) -> bool:
        return self._cold is not None

    @property
    def cold_path(self) -> str:
        return str(getattr(self._cold, "path", "") or "")

    def hot_cutoff(self) -> str:
        """热窗口起点（**含**）：``今天 - hot_days`` 的 ``"YYYY-MM-DD"``。

        早于该日期的 K 线判为**冷数据**（搬进冷仓，不再被每日同步刷新）。
        边界只在这里算一次，是全模块（以及冷热判定）的唯一真源。
        """
        return to_iso(local_now() - timedelta(days=self.hot_days))[:10]

    @staticmethod
    def _dt_date(dt) -> str:
        """把 dt 规范成 ``"YYYY-MM-DD"`` 以便与热窗口边界做**字典序**比较。

        兼容两种落库格式（历史遗留，见 V9 迁移 v14）：``YYYY-MM-DD[ 时间]`` 与
        ``YYYYMMDD``。取前 8 位数字重组；**不足 8 位数字 → 返回 ""**，此时
        ``"" < cutoff`` 恒成立 ⇒ 判为冷。这是刻意的：格式坏掉的行绝不该留在热表里
        被每天反复刷新（那既刷不出正确数据，又白占热表）。
        """
        digits = "".join(ch for ch in str(dt or "") if ch.isdigit())[:8]
        if len(digits) < 8:
            return ""
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"

    def _is_hot(self, dt: str) -> bool:
        """dt 是否落在热窗口内（写入热表）。按**日期**比较，不依赖年份。"""
        return self._dt_date(dt) >= self.hot_cutoff()

    def _where(self, table: str, code: str, period: str, adjust: str = "") -> str:
        # M6：复权维度入唯一键后，读路径必须按 adjust 过滤，
        # 否则 qfq/hfq/原始价三份数据混排（同一 dt 多根 K 线）。
        return (f"SELECT dt, open, high, low, close, volume, amount, adjust FROM {table} "
                f"WHERE code=? AND period=? AND adjust=? ORDER BY dt DESC")

    def get(self, code: str, period: str, count: int, adjust: str = "") -> list[dict]:
        """取最近 count 根（热表优先 + 冷仓历史补足），按时间升序返回。

        对调用方**透明**：冷热分了几层是实现细节，消费方只看到一条连续序列。

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
            arch = self._arch.query(self._where(self._ARCHIVE, code, period, adj) + " LIMIT ?",
                                    (code, period, adj, need))
        rows = _rows_from(arch + hot)
        rows.sort(key=lambda x: str(x["time"] or ""))
        return rows

    def put(self, code: str, period: str, bars: list[dict], adjust: str = "") -> int:
        """写入/刷新 K 线（按热窗口路由热表/冷仓，单事务批量 upsert）。

        早于热窗口的历史落冷仓，热窗口内的数据落热表（由每日同步任务刷新）。
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
            self._arch.executemany_in_txn(
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
        a = self._arch.query_one(
            f"SELECT MAX(fetched_at) AS f FROM {self._ARCHIVE} "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adj))
        return max(float((row or {}).get("f") or 0.0),
                   float((a or {}).get("f") or 0.0))

    def count(self, code: str, period: str) -> int:
        if self.db is None:
            return 0
        row = self.db.query_one(
            f"SELECT COUNT(1) AS c FROM {self._HOT} WHERE code=? AND period=?",
            (code, period))
        a = self._arch.query_one(
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
            arch = await self._arch.aquery(
                self._where(self._ARCHIVE, code, period, adj) + " LIMIT ?",
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
            await self._arch.aexecutemany_in_txn(
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
        a = await self._arch.aquery_one(
            f"SELECT MAX(fetched_at) AS f FROM {self._ARCHIVE} WHERE code=? AND period=? AND adjust=?",
            (code, period, adj))
        return max(float((row or {}).get("f") or 0.0), float((a or {}).get("f") or 0.0))

    async def acount(self, code: str, period: str, adjust: str = "") -> int:
        if self.db is None:
            return 0
        adj = adjust or ""
        row = await self.db.aquery_one(
            f"SELECT COUNT(1) AS c FROM {self._HOT} WHERE code=? AND period=? AND adjust=?",
            (code, period, adj))
        a = await self._arch.aquery_one(
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
            rows += self._arch.query(
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

    _SERIES_SQL = ("SELECT code, period, COUNT(1) AS rows, MAX(fetched_at) AS last_fetch "
                   "FROM {table} GROUP BY code, period")

    @staticmethod
    def _merge_series(*groups: list[dict]) -> list[dict]:
        """把多张表的 (code, period) 计数合并成一份清单（行数相加、时间取最大）。"""
        merged: dict = {}
        for rows in groups:
            for r in rows or []:
                k = (r["code"], r["period"])
                m = merged.setdefault(
                    k, {"code": r["code"], "period": r["period"],
                        "rows": 0, "last_fetch": r.get("last_fetch")})
                m["rows"] += int(r.get("rows") or 0)
                if r.get("last_fetch") and m["last_fetch"]:
                    m["last_fetch"] = max(m["last_fetch"], r["last_fetch"])
                elif r.get("last_fetch"):
                    m["last_fetch"] = r["last_fetch"]
        return sorted(merged.values(), key=lambda x: (x["code"], x["period"]))

    def all_series(self) -> list[dict]:
        """列出缓存中全部 code×period 序列及行数/最近抓取时间（**热表 + 冷仓**）。

        冷热分了两层，但序列清单是**一份** —— 消费方（导出、状态页）看到的必须是
        「这个 code 一共有多少根」，而不是「热表里有多少根」。
        """
        if self.db is None:
            return []
        return self._merge_series(
            self.db.query(self._SERIES_SQL.format(table=self._HOT)),
            self._arch.query(self._SERIES_SQL.format(table=self._ARCHIVE)))

    def hot_series(self) -> list[dict]:
        """只列**热表**中的序列（每日定时刷新用）。

        「只更新热数据」的落地：定时同步按本清单回源，冷仓里的历史**完全不碰** ——
        既不重新下载（省流量/省时间），也不重写（省磁盘写放大）。
        用户真正翻到某个老标的时，``get_or_fetch`` 会按需回源补热窗口。
        """
        if self.db is None:
            return []
        return self._merge_series(
            self.db.query(self._SERIES_SQL.format(table=self._HOT)))

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

    # ---------------- 热窗口滚动维护 ----------------
    def archive_rollover(self) -> dict:
        """把热表中**早于热窗口**的行搬入冷仓（每日维护，幂等）。

        判定统一走 :meth:`_is_hot`（→ :meth:`hot_cutoff` → ``bars_hot_days``），
        不另写一套比较 —— 边界只允许有一份实现，否则「写入路由」与「滚动搬移」
        迟早用两个不同的边界，出现「刚搬走又被写回热表」的抖动。

        SQL 侧先用 ``dt < cutoff`` 粗筛（把要搬的行降到少量），Python 侧再用
        ``_is_hot`` 精确复核：粗筛是为了避免把整个热表拉进内存（历史实现拉的是
        一整年，现在是 3 个月，但仍然没必要全量物化），复核是为了兜住
        ``YYYYMMDD`` / ``YYYY-MM-DD`` 混排等格式差异。

        搬移目标 = :attr:`_arch`（冷仓可用即冷仓）。用 ``id`` 精确删除热行，
        不会误删窗口内的数据。返回 ``{"moved": N, "deleted": N}``。
        """
        if self.db is None:
            return {"moved": 0, "deleted": 0}
        cutoff = self.hot_cutoff()
        rows = self.db.query(
            f"SELECT id, code, period, dt, open, high, low, close, volume, amount, "
            f"fetched_at, adjust FROM {self._HOT} WHERE dt < ? OR dt < ?",
            (cutoff, cutoff.replace("-", "")))
        to_move = [r for r in rows if not self._is_hot(r["dt"])]
        if not to_move:
            return {"moved": 0, "deleted": 0}
        cols = ("code,period,dt," + ",".join(_FIELDS) + ",fetched_at,adjust")
        # ① 先写冷仓（幂等：UNIQUE 冲突时 REPLACE）
        self._arch.executemany_in_txn(
            f"INSERT OR REPLACE INTO {self._ARCHIVE} ({cols}) VALUES ({','.join('?' * 11)})",
            [(r["code"], r["period"], r["dt"], r["open"], r["high"], r["low"],
              r["close"], r["volume"], r["amount"], r["fetched_at"], r["adjust"])
             for r in to_move])
        # ② 再删热表（冷仓写成功才删 —— 中途失败只会留下重复行，绝不丢数据）
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
            a = self._arch.query_one(
                f"SELECT COUNT(1) AS c, COUNT(DISTINCT code||'|'||period) AS s "
                f"FROM {self._ARCHIVE}")
            archive_rows = int((a or {}).get("c") or 0)
            symbols += int((a or {}).get("s") or 0)
            total += archive_rows
        served = self.hits + self.misses + self.stale_serves
        return {"rows": total, "hot_rows": hot, "archive_rows": archive_rows,
                "series": symbols,
                # 冷热分层口径（前端/运维页据此展示「热窗口」与冷仓位置）
                "hot_days": self.hot_days, "hot_cutoff": self.hot_cutoff(),
                "cold_enabled": self.cold_enabled, "cold_path": self.cold_path,
                "hits": self.hits, "misses": self.misses,
                "stale_serves": self.stale_serves,
                "hit_rate": round(self.hits / served, 4) if served else None,
                "ttl_daily": self.ttl_daily, "ttl_intraday": self.ttl_intraday}

    def clear(self, code: str = "", period: str = "") -> int:
        """清空整表（或按 code/period）。热表与冷仓一并清理。"""
        if self.db is None:
            return 0
        n = 0
        # 热表在主库、归档在冷仓，各自用自己的宿主执行 DELETE。
        for host, table in ((self.db, self._HOT), (self._arch, self._ARCHIVE)):
            if code and period:
                cur = host.execute(f"DELETE FROM {table} WHERE code=? AND period=?",
                                   (code, period))
            elif code:
                cur = host.execute(f"DELETE FROM {table} WHERE code=?", (code,))
            else:
                cur = host.execute(f"DELETE FROM {table}")
            n += int(cur.rowcount or 0)
        return n
