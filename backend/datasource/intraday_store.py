"""分钟线 K 线仓（R28，独立 SQLite 文件）。

## 为什么分钟线不能进主库

全市场 1 分钟线约 **3 亿行/年**（约 5000 只 × 240 根/日 × 250 交易日）。把它和日线
一起塞进 ``app.db`` 会同时踩两个已实测的坑：

1. **主库膨胀拖垮运维**（TD-25）：主库 585MB 时，每次启动的全量备份要写 4.4GB、
   耗时 11 分钟，全量回归被迫关掉备份才能跑。
2. **WAL 写放大**：主库还承载订单 / 审计 / 任务等 OLTP 写入并开了 WAL，
   分钟线这种「只追加、体积占绝对多数」的数据会让每次 checkpoint 搬运量暴涨。

所以与 :mod:`datasource.cold_store` 同思路：独立文件、独立保留策略、可单独清理。
三者分工——

- ``app.db`` 主库：日/周/月线（``local_bars``）+ 全部 OLTP 表；
- ``bars_cold.db``：日线冷数据（超热窗口的历史，**永久保留**）；
- ``bars_intraday.db``：分钟线（**滚动保留**，默认 120~730 天按周期区分）。

## 时间列为什么拆成 dt + tm

分钟线的 ``time`` 在各源有至少四种形状（``"20261008093000"`` / ``"2026-10-08 09:30"``
/ ``datetime`` / ``"2026/10/08 09:30"``）。若原样落库就会重演 V11 R13 的事故：
同一列混存两种字符串 ⇒ 排序错乱、同日双行、区间过滤静默失效。

因此**入库即拆分**：``dt`` = ``YYYYMMDD``（走 :func:`core.clock.bar_date` 唯一入口），
``tm`` = ``HHMM``（4 位，零填充）。主键 ``(code, period, adjust, dt, tm)`` 天然幂等。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from core.clock import bar_date as _bar_date
from core.clock import now_iso as _now

log = logging.getLogger("qmt_work.datasource.intraday_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_bars_intraday (
    code TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '1m',
    adjust TEXT NOT NULL DEFAULT '',
    dt TEXT NOT NULL,
    tm TEXT NOT NULL DEFAULT '',
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    provider_id TEXT DEFAULT '',
    batch_id TEXT DEFAULT '',
    fetched_at TEXT DEFAULT '',
    PRIMARY KEY (code, period, adjust, dt, tm)
);
"""

#: 索引**必须**在建表 + ``batch_id`` 补列**之后**再建：旧分钟仓文件里没有
#: ``batch_id`` 列，此时建 ``idx_intraday_source`` 会直接 OperationalError。
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_intraday_lookup
    ON local_bars_intraday(code, period, adjust, dt);
-- 保留清理走 `WHERE dt < ?`：没有这个索引就是全表扫。
CREATE INDEX IF NOT EXISTS idx_intraday_dt ON local_bars_intraday(dt);
-- 与 local_bars 同口径：批次/来源回查（快照发布、同步溯源）走这个索引。
CREATE INDEX IF NOT EXISTS idx_intraday_source
    ON local_bars_intraday(provider_id, batch_id, dt);
"""

#: 从任意形状的 time 值里抠出 ``HHMM``。命中失败返回 ``""``（不猜、不补零）。
_TM_PATTERNS = (
    re.compile(r"(?<=\d{8})(\d{4})"),          # 20261008093000 / 202610080930
    re.compile(r"\d{4}-\d{2}-\d{2}[ T](\d{2}):(\d{2})"),
    re.compile(r"\d{4}/\d{2}/\d{2}[ T](\d{2}):(\d{2})"),
)


def _valid_yyyymmdd(s: str) -> bool:
    """``YYYYMMDD`` 粗校验：8 位数字 + 月日在合理区间（不校验交易日）。"""
    if len(s) != 8 or not s.isdigit():
        return False
    return 1 <= int(s[4:6]) <= 12 and 1 <= int(s[6:8]) <= 31


def split_dt_tm(value: Any) -> tuple[str, str]:
    """把任意形状的 K 线时间拆成 ``(dt="YYYYMMDD", tm="HHMM")``。

    ★ **不能只靠** :func:`core.clock.bar_date`：它只认 8 位日期与 ISO 串，而分钟线
    源最常见的形状恰恰是 **14 位紧凑时间戳** ``"20261008093000"``（实测：直接喂给
    ``bar_date`` 返回 ``""``，导致整批分钟线被静默丢弃并计为「同步成功 0 行」——
    这正是「假成功」家族的新成员）。故此处**先**处理紧凑数字串，再回退 ``bar_date``。

    ``tm`` 取不到时返回 ``""`` 而不是 ``"0000"`` —— ``""`` 能被明确识别为「无分钟
    精度」（日线就是这种情况），``"0000"`` 则会伪装成「当天 0 点的一根分钟线」。
    日期解析不出时整体返回 ``("", "")``，由调用方丢弃该行。
    """
    # ① 紧凑数字串：YYYYMMDD[HHMM[SS]]（14 / 12 / 8 位）
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit() and len(s) >= 8:
            head = s[:8]
            if _valid_yyyymmdd(head):
                return head, (s[8:12] if len(s) >= 12 else "")
        # ①b 带分隔符：2026-10-08 09:30 / 2026/10/08 09:30 / 2026-10-08
        #     （bar_date 不认「斜杠日期 + 时间」的组合，会整行判为非法 ⇒ 静默丢数据）
        m = re.match(r"^(\d{4})[-/.](\d{2})[-/.](\d{2})(?:[ T](\d{2}):(\d{2}))?", s)
        if m:
            head = m.group(1) + m.group(2) + m.group(3)
            if _valid_yyyymmdd(head):
                return head, ((m.group(4) + m.group(5)) if m.group(4) else "")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        s = str(int(value))
        if len(s) >= 8 and _valid_yyyymmdd(s[:8]):
            return s[:8], (s[8:12] if len(s) >= 12 else "")

    # ② 其余形状交回唯一时钟入口
    dt = _bar_date(value)
    if not dt:
        return "", ""
    if isinstance(value, str):
        for pat in _TM_PATTERNS:
            m = pat.search(value)
            if m:
                g = m.groups()
                tm = g[0] if len(g) == 1 else (g[0] + g[1])
                if len(tm) >= 4:
                    return dt, tm[:4]
                break
    # datetime / date 对象：bar_date 已给出日期，这里补时间部分
    hh = getattr(value, "hour", None)
    mm = getattr(value, "minute", None)
    if hh is not None and mm is not None:
        return dt, f"{int(hh):02d}{int(mm):02d}"
    return dt, ""


def _num(v: Any) -> Optional[float]:
    """数值安全转换：None/''/非法 → None（绝不估算填充）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class IntradayStore:
    """分钟线仓（独立 SQLite 文件）。写经 ``_lock`` 串行，读走只读连接。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=8000")
        except sqlite3.Error as exc:  # noqa: BLE001 只读介质等场景降级，不阻塞启动
            from core.errors import swallow
            swallow(exc, why="分钟线仓 PRAGMA 设置失败（只读介质？）；"
                             "降级为默认 journal/synchronous，不阻塞启动",
                    logger=log)
        self._ro: Any = None
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 兼容本次开发期内已生成的分钟仓文件：缺列则补，已有则跳过。
            cols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(local_bars_intraday)").fetchall()}
            if "batch_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE local_bars_intraday "
                    "ADD COLUMN batch_id TEXT DEFAULT ''")
            # 索引放最后建：idx_intraday_source 依赖 batch_id 列必须已存在。
            self._conn.executescript(_INDEXES)
            self._conn.commit()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def upsert_bars(self, code: str, bars: Sequence[Union[dict, Any]],
                    period: str = "1m", adjust: str = "",
                    provider_id: str = "",
                    batch_id: str = "", schema_version: str = "",
                    quality_state: str = "") -> int:
        """批量写入分钟线。返回**实际写入行数**（日期无法解析的行被丢弃）。

        与 ``local_bars`` 一样是 ``INSERT OR REPLACE``：重复时间点覆盖、旧数据不删。

        ★ ``batch_id`` / ``schema_version`` / ``quality_state`` **必须接收**：
        :class:`app.sync.bars.BarsSyncer` 的落库契约对主库和分钟仓**同一个调用**
        （``upsert_bars(code, bars, period=, adjust=, provider_id=, batch_id=,
        schema_version=, quality_state=)``）。这里不接受就会就地
        ``TypeError: unexpected keyword argument``，**所有分钟线数据集同步
        100% 落库失败**，而调用方只会看到一句「落库失败」——签名漂移是最阴
        险的断链。``batch_id`` 真实落库（批次 / 来源回查用）；
        ``schema_version`` / ``quality_state`` 显式接收但**不存**——分钟仓刻意保持
        精瘦，数据质量审计链只跟日线走。明说不存比靠 ``**kwargs`` 悄悄吞掉安全。
        """
        rows: list[tuple] = []
        for b in bars or []:
            d = b.model_dump() if hasattr(b, "model_dump") else dict(b)
            dt, tm = split_dt_tm(d.get("time"))
            if not dt:
                log.warning("intraday upsert 跳过时间无法解析的 K 线：code=%s time=%r",
                            code, d.get("time"))
                continue
            rows.append((
                code, period, adjust, dt, tm,
                _num(d.get("open")), _num(d.get("high")), _num(d.get("low")),
                _num(d.get("close")), _num(d.get("volume")), _num(d.get("amount")),
                provider_id or "", batch_id or "", _now(),
            ))
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO local_bars_intraday "
                "(code,period,adjust,dt,tm,open,high,low,close,volume,amount,"
                "provider_id,batch_id,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _read_conn(self):
        if self._ro is None:
            try:
                conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                       timeout=15.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=8000")
                self._ro = conn
            except sqlite3.Error:
                self._ro = False
        return self._ro or None

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """只读查询。优先走只读连接；不可用时退主连接并持锁（避免并发 execute）。"""
        ro = self._read_conn()
        if ro is not None:
            return [dict(r) for r in ro.execute(sql, params).fetchall()]
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _scalar(self, sql: str, params: tuple = (), key: str = "") -> Any:
        ro = self._read_conn()
        if ro is not None:
            row = ro.execute(sql, params).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return row[key] if key else row[0]

    def get_bars(self, code: str, period: str = "1m", adjust: str = "",
                 limit: int = 500, start: str = "", end: str = "") -> list[dict]:
        """取分钟线（按 dt/tm 升序）。``start``/``end`` 走 :func:`bar_date` 归一。"""
        sql = ("SELECT dt, tm, open, high, low, close, volume, amount, provider_id "
               "FROM local_bars_intraday WHERE code=? AND period=? AND adjust=?")
        params: list[Any] = [code, period, adjust]
        s = _bar_date(start) if start else ""
        e = _bar_date(end) if end else ""
        if s:
            sql += " AND dt >= ?"
            params.append(s)
        if e:
            sql += " AND dt <= ?"
            params.append(e)
        sql += " ORDER BY dt ASC, tm ASC LIMIT ?"
        params.append(int(limit))
        return self._query(sql, tuple(params))

    def latest_dt(self, code: str, period: str = "1m", adjust: str = "") -> str:
        """该标的该周期最新一根的交易日（``""`` = 无数据）。增量跳过的游标。"""
        return self._scalar(
            "SELECT MAX(dt) AS d FROM local_bars_intraday "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adjust), "d") or ""

    def earliest_dt(self, code: str, period: str = "1m", adjust: str = "") -> str:
        """该标的该周期最早一根的交易日（``""`` = 无数据）。全量回补的断点游标。"""
        return self._scalar(
            "SELECT MIN(dt) AS d FROM local_bars_intraday "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adjust), "d") or ""

    # ------------------------------------------------------------------
    # 批量游标（与 LocalStore 同签名 ⇒ BarsSyncer 可直接复用，不写第二套断点逻辑）
    # ------------------------------------------------------------------
    def earliest_dt_map(self, codes: Sequence[str], period: str = "1m",
                        adjust: str = "") -> dict[str, str]:
        """``{code: 最早交易日}``。全量回补的断点游标——「数据即游标」，
        不需要额外的进度表，中断重跑时已补齐的自动跳过。
        """
        return self._dt_map(codes, period, adjust, "MIN")

    def latest_dt_map(self, codes: Sequence[str], period: str = "1m",
                      adjust: str = "") -> dict[str, str]:
        """``{code: 最新交易日}``。增量模式下「已够新则跳过」的判据。"""
        return self._dt_map(codes, period, adjust, "MAX")

    def provider_counts(self, codes: Sequence[str] | None = None,
                        period: str = "1m", adjust: str = "",
                        batch_id: str = "") -> dict[str, int]:
        """按 ``provider_id`` 统计分钟线行数 → ``{源名: 行数}``。

        与 :meth:`LocalStore.provider_counts` 同语义（真实溯源，不拿请求时的
        ``source="auto"`` 冒充）。给了 ``batch_id`` 就只数该批次 —— 否则「本次
        同步」会统计成「库里全部历史」，那是另一种假数字。
        """
        where = "WHERE period=? AND adjust=?"
        params: list[Any] = [period, adjust]
        if batch_id:
            where += " AND batch_id=?"
            params.append(batch_id)
        if codes:
            uniq = [str(c) for c in dict.fromkeys(codes) if c]
            if not uniq:
                return {}
            ph = ",".join("?" for _ in uniq)
            where += f" AND code IN ({ph})"
            params.extend(uniq)
        rows = self._query(
            f"SELECT provider_id AS p, COUNT(*) AS n FROM local_bars_intraday "
            f"{where} GROUP BY provider_id", tuple(params))
        return {str(r.get("p") or ""): int(r.get("n") or 0) for r in rows}

    def _dt_map(self, codes: Sequence[str], period: str, adjust: str,
                agg: str) -> dict[str, str]:
        codes = [c for c in (codes or []) if c]
        if not codes:
            return {}
        # SQLite 变量数上限（默认 999）兜底：分批查，超大批次也不会 SQLITE_ERROR。
        out: dict[str, str] = {}
        chunk = 500
        sql = (f"SELECT code, {agg}(dt) AS d FROM local_bars_intraday "
               "WHERE period=? AND adjust=? AND code IN ({}) GROUP BY code")
        for i in range(0, len(codes), chunk):
            part = codes[i:i + chunk]
            ph = ",".join("?" * len(part))
            rows = self._query(sql.format(ph), (period, adjust, *part))
            for r in rows:
                if r.get("d"):
                    out[str(r["code"])] = str(r["d"])
        return out

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def prune_before(self, dt: str, period: str = "") -> int:
        """删除早于 ``dt`` 的行（保留窗口清理）。``period`` 为空则不限周期。

        返回删除行数。``dt`` 非法时返回 0 并记 warning —— **绝不**因为参数问题
        就全表清空（``WHERE dt < ''`` 会命中所有行）。
        """
        d = _bar_date(dt)
        if not d:
            log.warning("prune_before 跳过非法日期：%r", dt)
            return 0
        with self._lock:
            if period:
                cur = self._conn.execute(
                    "DELETE FROM local_bars_intraday WHERE dt < ? AND period=?", (d, period))
            else:
                cur = self._conn.execute(
                    "DELETE FROM local_bars_intraday WHERE dt < ?", (d,))
            n = cur.rowcount or 0
            self._conn.commit()
        if n:
            log.info("分钟线保留清理：删除 %d 行（dt < %s, period=%s）", n, d, period or "*")
        return n

    def count(self, period: str = "") -> int:
        if period:
            n = self._scalar("SELECT COUNT(*) AS c FROM local_bars_intraday "
                             "WHERE period=?", (period,), "c")
        else:
            n = self._scalar("SELECT COUNT(*) AS c FROM local_bars_intraday", (), "c")
        return int(n or 0)

    def stats(self) -> dict:
        """按周期的行数与覆盖日期区间（供状态面板展示，不估算缺失）。"""
        rows = self._query(
            "SELECT period, COUNT(*) AS c, MIN(dt) AS d0, MAX(dt) AS d1, "
            "COUNT(DISTINCT code) AS codes FROM local_bars_intraday "
            "GROUP BY period ORDER BY period")
        return {
            "path": str(self.path),
            "total_rows": sum(int(r["c"]) for r in rows),
            "periods": {
                str(r["period"]): {
                    "rows": int(r["c"]), "codes": int(r["codes"]),
                    "first_dt": r["d0"] or "", "last_dt": r["d1"] or "",
                } for r in rows
            },
        }

    def close(self) -> None:
        from core.errors import swallow
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error as exc:
            swallow(exc, why="关闭分钟线仓写连接失败（已关闭/被占用）；进程退出路径不再重试",
                    logger=log)
        if self._ro:
            try:
                self._ro.close()
            except sqlite3.Error as exc:
                swallow(exc, why="关闭分钟线仓只读连接失败；进程退出路径不再重试",
                        logger=log)


_singleton: Optional[IntradayStore] = None
_singleton_lock = threading.Lock()


def get_intraday_store(path: Optional[Path | str] = None) -> IntradayStore:
    """进程级单例。``path`` 缺省时按 :func:`core.config.intraday_bars_path` 解析。

    刻意做成「惰性 + 可显式传路径」：测试要指向 tmp 目录，生产要跟随主库目录，
    两者不能共用同一个构造时机。
    """
    global _singleton
    if path is not None:
        return IntradayStore(path)
    with _singleton_lock:
        if _singleton is None:
            from core.config import intraday_bars_path
            _singleton = IntradayStore(intraday_bars_path())
        return _singleton


__all__ = ["IntradayStore", "get_intraday_store", "split_dt_tm"]
