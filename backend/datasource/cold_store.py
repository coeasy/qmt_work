"""K 线冷数据仓：超过热窗口（默认 3 个月）的历史 K 线，**独立 SQLite 文件**存放。

## 为什么是独立文件，而不是同库里的另一张表

主库（``app.db``）同时承载订单 / 审计 / 任务 / 告警等 OLTP 写入，且开了 WAL。
冷 K 线是**只追加、几乎不更新、体积占绝对多数**的数据；留在主库会让主库体积与
WAL 一起膨胀，每次 ``wal_checkpoint`` 都要搬运冷数据 —— 冷数据把热路径的写放大
整体抬高。拆出去之后：

- 主库只留热窗口（默认 92 天），checkpoint 只搬热数据；
- 冷数据可**单独备份 / 单独清理 / 单独挪到慢盘**，不影响主库；
- 冷文件的表结构只有一张表，不跑主库那套迁移账本。

## 与 ``KlineCache`` 的分工（唯一真源，不重复判定）

- ``KlineCache`` 持有「哪根算冷」的判定与路由（``hot_cutoff`` / ``_is_hot``），
  是**唯一真源**；
- 本模块只负责**存取**：建表、批量写入、查询、计数、把主库里的历史行一次性搬过来。

两处各判一次「冷热」迟早会漂移，所以这里**不**提供任何边界判定函数。

## 接口契约

公开方法刻意与 :class:`core.db.DB` 的**子集同名同签名**（``query`` / ``query_one`` /
``executemany_in_txn`` / ``aquery`` / ``aquery_one`` / ``aexecutemany_in_txn``），
于是 ``KlineCache`` 里访问归档表只需把 ``self.db`` 换成 ``self._cold``，
调用点一行都不用改 —— 少一处改写就少一处漂移。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("qmt_work.datasource.cold_store")

#: 冷仓唯一的表。列定义与主库 ``kline_archive`` **逐列一致**（含 M6 的
#: ``UNIQUE(code, period, adjust, dt)``），否则搬移时会出现「主库有的列冷库没有」。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS kline_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '1d',
    dt TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    fetched_at REAL DEFAULT 0,
    adjust TEXT DEFAULT '',
    UNIQUE(code, period, adjust, dt)
);
CREATE INDEX IF NOT EXISTS idx_kline_archive_lookup
    ON kline_archive(code, period, adjust, dt);
"""

#: 搬移时逐列点名（不用 ``SELECT *``）：列顺序/缺失列的问题在编译期就暴露，
#: 而不是搬出一堆错位的值。
_ARCHIVE_COLS = ("code", "period", "dt", "open", "high", "low", "close",
                 "volume", "amount", "fetched_at", "adjust")


class ColdStore:
    """冷 K 线仓（独立 SQLite 文件）。线程安全：写经 ``_lock`` 串行，读走只读连接。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        # 与主库同口径：WAL 让读写并发不互斥；synchronous=NORMAL 在 WAL 下仍不丢已提交事务。
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:  # noqa: BLE001 只读介质等场景降级不阻塞启动
            pass
        self._ro: Any = None            # 惰性只读连接；False = 不可用
        self._closed = False
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    # 读（与 core.db.DB.query 同签名）
    # ------------------------------------------------------------------
    def _read_conn(self):
        """惰性创建只读连接（URI mode=ro）；失败置 False 永久回退主连接。"""
        if self._ro is None:
            try:
                conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                       timeout=10.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                self._ro = conn
            except sqlite3.Error:
                self._ro = False
        return self._ro or None

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        rows = None
        ro = self._read_conn()
        if ro is not None:
            try:
                rows = ro.execute(sql, params).fetchall()
            except sqlite3.Error:
                rows = None      # RO 连接异常 → 回退主连接
        if rows is None:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    async def aquery(self, sql: str, params: tuple = ()) -> list[dict]:
        return await asyncio.to_thread(self.query, sql, params)

    async def aquery_one(self, sql: str, params: tuple = ()) -> dict | None:
        return await asyncio.to_thread(self.query_one, sql, params)

    # ------------------------------------------------------------------
    # 写（与 core.db.DB.executemany_in_txn 同签名）
    # ------------------------------------------------------------------
    def executemany_in_txn(self, sql: str, seq: Iterable[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq or [])
            self._conn.commit()

    async def aexecutemany_in_txn(self, sql: str, seq: Iterable[tuple]) -> None:
        await asyncio.to_thread(self.executemany_in_txn, sql, seq)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """单条写（返回游标，调用方可读 ``rowcount``）。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # ------------------------------------------------------------------
    # 运维 / 一次性搬移
    # ------------------------------------------------------------------
    def count(self) -> int:
        try:
            row = self.query_one("SELECT COUNT(1) AS c FROM kline_archive")
        except sqlite3.Error:
            return 0
        return int((row or {}).get("c") or 0)

    def migrate_from(self, db, table: str = "kline_archive",
                     batch: int = 5000) -> dict:
        """把主库里遗留的 ``kline_archive`` 行一次性搬进冷仓，**按批**搬运。

        迁移前：历史 K 线存在主库的同名表里（V9 迁移 v14/v18 的产物）。
        迁移后：该表在主库中**保持为空**（本函数不 DROP，保留空表以便回滚），
        冷仓成为历史 K 线的唯一存放处。

        幂等：主库表为空 / 表不存在 / 已是空表 ⇒ 直接返回 0，无副作用。
        **崩溃安全**：每批先写冷仓并 commit，再删主库对应行并 commit；
        任一批失败即停止并返回已完成的批数 —— 重跑会从剩下的行继续，
        不会出现「删了主库但冷仓没写进去」的数据丢失窗口。

        返回 ``{"moved": n, "batches": k, "done": bool}``。
        """
        cols = ",".join(_ARCHIVE_COLS)
        placeholders = ",".join("?" * len(_ARCHIVE_COLS))
        moved = 0
        batches = 0
        while True:
            try:
                rows = db.query(
                    f"SELECT {cols} FROM {table} ORDER BY id LIMIT ?", (int(batch),))
            except sqlite3.Error as exc:
                log.info("冷仓搬移：主库 %s 不可读（%s），视为无需搬移", table, exc)
                return {"moved": moved, "batches": batches, "done": True}
            if not rows:
                return {"moved": moved, "batches": batches, "done": True}
            seq = [tuple(r[c] for c in _ARCHIVE_COLS) for r in rows]
            # ① 先写冷仓（幂等：UNIQUE 冲突时 REPLACE）
            self.executemany_in_txn(
                f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", seq)
            # ② 再删主库（按 (code,period,adjust,dt) 精确定位，不用 id —— id 是主库内部行号）
            with self._lock:
                db.executemany_in_txn(
                    f"DELETE FROM {table} WHERE code=? AND period=? AND dt=? AND adjust=?",
                    [(r["code"], r["period"], r["dt"], r["adjust"]) for r in rows])
            moved += len(rows)
            batches += 1
            log.info("冷仓搬移：已搬 %d 行（第 %d 批）", moved, batches)
            if len(rows) < int(batch):
                return {"moved": moved, "batches": batches, "done": True}

    def copy_from_file(self, src: Path | str, batch: int = 5000) -> dict:
        """把**另一个冷仓文件**里的行复制进来（**不删源**）。

        场景（P0-3 II）：用户在设置里改了冷库目录 ⇒ 新冷库是空的 ⇒ 图表历史
        **静默变短**（不报错、不提示，最难查的那类问题）。这个函数就是那个
        「把旧库搬过来」的动作。

        ★ 刻意**不删源文件**：目录是用户自己选的，删错一个文件比多占一份磁盘
        严重得多；复制完成后由用户确认无误再自行清理，界面会明示源路径与保留事实。

        返回 ``{"moved": n, "batches": k, "src": str, "dst": str}``；
        源文件不存在 / 打不开 ⇒ 返回 ``moved=0`` 并带 ``error``（不抛异常，
        调用方要能把失败如实告知用户，而不是把异常翻译成 500）。
        """
        src_p = Path(src)
        out = {"moved": 0, "batches": 0, "src": str(src_p), "dst": str(self.path),
               "error": ""}
        if not src_p.exists() or not src_p.is_file():
            out["error"] = f"源文件不存在：{src_p}"
            return out
        if src_p.resolve() == Path(self.path).resolve():
            out["error"] = "源与目标相同，无需迁移"
            return out
        cols = ",".join(_ARCHIVE_COLS)
        placeholders = ",".join("?" * len(_ARCHIVE_COLS))
        try:
            sconn = sqlite3.connect(f"file:{src_p}?mode=ro", uri=True, timeout=10.0)
            sconn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            out["error"] = f"源冷仓不可读：{exc}"
            return out
        try:
            offset = 0
            while True:
                try:
                    rows = sconn.execute(
                        f"SELECT {cols} FROM kline_archive ORDER BY id LIMIT ? OFFSET ?",
                        (int(batch), offset)).fetchall()
                except sqlite3.Error as exc:
                    # 源里根本没有这张表（比如用户指错了目录）⇒ 视为 0 行，不是错误
                    out["error"] = f"源冷仓无 kline_archive 表（{exc}）"
                    break
                if not rows:
                    break
                seq = [tuple(r[c] for c in _ARCHIVE_COLS) for r in rows]
                self.executemany_in_txn(
                    f"INSERT OR REPLACE INTO kline_archive ({cols}) VALUES ({placeholders})",
                    seq)
                out["moved"] += len(rows)
                out["batches"] += 1
                if len(rows) < int(batch):
                    break
                offset += len(rows)
        finally:
            try:
                sconn.close()
            except sqlite3.Error:
                pass
        return out

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            for conn in (self._ro if self._ro not in (None, False) else None,
                         self._conn):
                if conn is None:
                    continue
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._ro = False
        return True


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_cold: Optional[ColdStore] = None
_cold_lock = threading.Lock()


def get_cold_store() -> Optional[ColdStore]:
    """取冷仓单例；未初始化返回 ``None``（调用方须能接受「无冷仓」并降级）。

    刻意**不**在这里惰性建库：建库时机由启动阶段显式决定（便于在测试里换成
    ``tmp_path`` 下的文件），避免「第一次查询顺手建了个库」这种隐式副作用。
    """
    return _cold


def init_cold_store(path: Path | str) -> ColdStore:
    """初始化冷仓单例（启动阶段调用；重复调用幂等复用同一个实例）。"""
    global _cold
    with _cold_lock:
        if _cold is None:
            _cold = ColdStore(path)
        return _cold


def reset_cold_store() -> None:
    """关闭并清空单例（测试 / 停机用）。"""
    global _cold
    with _cold_lock:
        if _cold is not None:
            try:
                _cold.close()
            except Exception:  # noqa: BLE001
                pass
        _cold = None


__all__ = ["ColdStore", "get_cold_store", "init_cold_store", "reset_cold_store"]
