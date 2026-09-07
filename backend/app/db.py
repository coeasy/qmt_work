"""SQLite 数据层：版本化迁移 + 最小 CRUD（§4.11 数据模型，工业级演进）。

- 建表采用**版本化迁移**：schema_migrations 记录已应用版本，新增表/字段只需追加迁移
  （旧库自动升级，无需手工删库）。
"""
import asyncio
import hashlib
import json
import logging
import contextlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("qmt_work.db")

# 迁移与扩展列定义拆至 app/db_migrations.py（2026-09 第三期）；
# 模块级名字保持 _MIGRATIONS/_EXTRA_COLUMNS，既有 monkeypatch（tests/test_unit.py）不受影响。
from app.db_migrations import EXTRA_COLUMNS as _EXTRA_COLUMNS, MIGRATIONS as _MIGRATIONS



# 参与审计 hash 计算的字段（顺序固定，改动会使旧链失效）
_AUDIT_HASH_FIELDS = ("actor", "api_key_id", "action", "target",
                      "params_json", "result", "ip", "created_at")

_lock = threading.Lock()
_audit_lock = threading.Lock()


class _RWLock:
    """读写锁（M3）：读共享、写独占、写优先防饥饿。

    背景：原实现所有读写共用一把 threading.Lock —— WAL 虽让 SQLite 层面
    读写可并发，但应用层互斥把读也串行化了，长事务（行情微批写盘/备份）
    期间所有查询排队。现拆为读写锁：查询走 read（共享），写/迁移/备份走
    write（独占）。

    安全前提：共享连接并发读要求 sqlite3 serialized 模式（threadsafety>=3，
    CPython 默认满足）。若运行环境 threadsafety<3，read() 自动降级为独占，
    行为与旧实现完全一致。
    """

    def __init__(self, concurrent_reads: bool = True):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0
        self._concurrent = concurrent_reads

    @contextlib.contextmanager
    def read(self):
        if not self._concurrent:
            with self.write():
                yield
            return
        with self._cond:
            while self._writer or self._writers_waiting:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write(self):
        with self._cond:
            self._writers_waiting += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._writers_waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


_rw = _RWLock(concurrent_reads=sqlite3.threadsafety >= 3)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def audit_chain_hash(prev_hash: str, row: dict) -> str:
    """D4：审计记录链式哈希 = sha256(prev_hash | 各字段值)。

    任何一条历史记录被篡改（或被删除），其后所有记录的 hash 都无法自洽，
    `/audit/verify` 会定位到第一处断链。
    """
    parts = [prev_hash or ""]
    for f in _AUDIT_HASH_FIELDS:
        v = row.get(f)
        parts.append("" if v is None else str(v))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _split_statements(sql: str) -> list[str]:
    """把迁移脚本按分号切成单条 SQL（本库迁移脚本的字符串字面量不含分号，安全切分）。"""
    return [s.strip() for s in sql.split(";") if s.strip()]


class DB:
    """极简 SQLite 封装：线程安全、版本化迁移、自动建表。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        # M3 读写分离：读走独立只读连接（WAL 允许「一写多读」并发；同一连接上
        # 写提交会打断本连接未完成的读游标——这是原实现全量互斥的根因）。
        # _ro=None 未初始化 / False=不可用（内存库等）→ 查询回退主连接写锁。
        self._ro = None if str(path) != ":memory:" else False
        # WAL 模式：读写并发不互斥（审计/K线缓存/快照高频写时读不阻塞），
        # 崩溃恢复更稳；synchronous=NORMAL 在 WAL 下仍保证不丢已提交事务。
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:  # noqa: BLE001 只读介质等场景降级不阻塞启动
            pass
        self._audit_last_hash: str | None = None   # D4 审计链尾哈希（惰性加载）
        self._migrate()
        self._ensure_columns()
        self._conn.commit()

    def _migrate(self) -> None:
        """按版本应用未执行的迁移（阶段 3：事务化 + 失败回滚）。

        原实现用 executescript 逐个执行——executescript 执行前会隐式 COMMIT，
        且多条语句间不共享事务；某条中途失败会留下半成品 schema，但版本号未写入，
        下次启动还会再跑、继续失败（半迁移僵尸态）。

        现改为：每条迁移的全部语句 + 版本号写入放在**同一个事务**里，
        任一步失败即 ROLLBACK 整体回滚，保证「要么完整应用、要么完全未应用」。
        """
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {r[0] for r in self._conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in sorted(_MIGRATIONS):
            if version in done:
                continue
            with _rw.write():
                # 显式事务包裹（含 DDL）：任一步失败即整体回滚
                self._conn.execute("BEGIN")
                try:
                    for stmt in _split_statements(sql):
                        self._conn.execute(stmt)
                    self._conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) "
                        "VALUES (?, ?)", (version, now_iso()))
                    self._conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    log.warning("迁移 v%d 失败并回滚：%s", version, exc)
                    raise

    def _ensure_columns(self) -> None:
        """幂等补充各表的向后兼容扩展字段（旧库自动升级，不需删库）。"""
        for table, extras in _EXTRA_COLUMNS.items():
            try:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if not cols:
                continue
            for c in extras:
                if c not in cols:
                    with _rw.write():
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {c} TEXT DEFAULT ''")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with _rw.write():
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        """批量写（同锁 + 单事务提交），供本地数据仓等批量导入场景使用。"""
        if not seq:
            return
        with _rw.write():
            self._conn.executemany(sql, seq)
            self._conn.commit()

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
        # M3 读写锁：查询共享持有（走 RO 连接），写事务期间读不再排队。
        rows = None
        with _rw.read():
            ro = self._read_conn()
            if ro is not None:
                try:
                    rows = ro.execute(sql, params).fetchall()
                except sqlite3.Error:
                    rows = None  # RO 连接异常：释放读锁后回退主连接
        if rows is None:
            # 回退路径：主连接上的读必须与写互斥（同连接写提交会打断读游标）
            with _rw.write():
                rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, data: dict) -> int:
        keys = list(data.keys())
        sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        with _rw.write():
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def upsert(self, table: str, data: dict) -> int:
        """INSERT OR REPLACE：依赖表上的 UNIQUE 约束（如 market_cache 的 code/dtype/ts）。

        重复爬取/重复 tick 时刷新数据而非报错。
        """
        keys = list(data.keys())
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        with _rw.write():
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    # ---------------- 阶段 3：异步包装（同步 sqlite 移出事件循环） ----------------
    # 事件循环内的 async 代码应调用 a* 变体，把阻塞的 sqlite 调用 offload 到线程池，
    # 避免高频写入（行情缓存/快照/通知/投递日志）卡住事件循环。
    # 底层已用全局 threading.Lock + check_same_thread=False，线程池安全。

    async def aexecute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return await asyncio.to_thread(self.execute, sql, params)

    async def aquery(self, sql: str, params: tuple = ()) -> list[dict]:
        return await asyncio.to_thread(self.query, sql, params)

    async def aquery_one(self, sql: str, params: tuple = ()) -> dict | None:
        return await asyncio.to_thread(self.query_one, sql, params)

    async def ainsert(self, table: str, data: dict) -> int:
        return await asyncio.to_thread(self.insert, table, data)

    async def aupsert(self, table: str, data: dict) -> int:
        return await asyncio.to_thread(self.upsert, table, data)

    # ---------------- C1/P2-1：批量事务写入 + market_cache 保留策略 ----------------
    def executemany_in_txn(self, sql: str, seq) -> None:
        """单事务批量执行（一条 commit），用于行情写盘微批化，把「每 tick 一 commit」
        降为「每窗口一 commit」，显著降低高频订阅多标的时的 fsync/QPS 压力。"""
        with _rw.write():
            self._conn.executemany(sql, seq or [])
            self._conn.commit()

    async def aexecutemany_in_txn(self, sql: str, seq) -> None:
        """executemany_in_txn 的异步线程池包装（事件循环内调用，避免阻塞）。"""
        await asyncio.to_thread(self.executemany_in_txn, sql, seq)

    def prune_market_cache(self, keep: int = 20) -> int:
        """逐 code+dtype 仅保留最近 ``keep`` 个 ts 的行，防止 market_cache 无界增长。

        用 id 倒序取保留集（内部实现保证 id 与插入顺序单调），删除更旧的 tick 行。
        返回本次清理的行数。
        """
        want = int(keep)
        if want < 1:
            want = 20
        removed = 0
        with _rw.write():
            groups = self._conn.execute(
                "SELECT code, dtype, COUNT(*) FROM market_cache "
                "GROUP BY code, dtype HAVING COUNT(*) > ?", (want,)).fetchall()
            for code, dtype, _cnt in groups:
                keep_ids = [r[0] for r in self._conn.execute(
                    "SELECT id FROM market_cache WHERE code=? AND dtype=? "
                    "ORDER BY id DESC LIMIT ?", (code, dtype, want))]
                if not keep_ids:
                    continue
                ph = ",".join("?" * len(keep_ids))
                cur = self._conn.execute(
                    f"DELETE FROM market_cache "
                    f"WHERE code=? AND dtype=? AND id NOT IN ({ph})",
                    (code, dtype, *keep_ids))
                removed += int(cur.rowcount or 0)
            self._conn.commit()
            return removed

    async def aprune_market_cache(self, keep: int = 20) -> int:
        return await asyncio.to_thread(self.prune_market_cache, keep)

    # ---------------- 阶段 3：一致性备份（sqlite3 backup API） ----------------
    def backup_to(self, dst: Path) -> bool:
        """用 sqlite3 backup API 生成**一致性**备份（含 WAL 中已提交但未 checkpoint 的数据）。

        相比逐文件 copy 主库 + -wal/-shm（可能拿到中间状态、且重启时 -wal 失效），
        `Connection.backup()` 在事务层面拷贝出单一完整文件，可直接单独使用/还原。
        备份期间持有全局锁，避免写入并发导致快照不一致。
        """
        try:
            dst = Path(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(dst) + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
            dst_conn = sqlite3.connect(tmp)
            try:
                with _rw.write():
                    self._conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
            os.replace(tmp, dst)   # 原子落位：进程崩溃不会留下半份备份
            return True
        except sqlite3.Error as exc:
            log.warning("sqlite backup API 失败：%s", exc)
            return False

    # ---------------- 审计日志（E4 脱敏 + D4 hash 链） ----------------
    def _last_audit_hash(self) -> str:
        if self._audit_last_hash is None:
            row = self.query_one(
                "SELECT hash FROM audit_log WHERE hash IS NOT NULL AND hash!='' "
                "ORDER BY id DESC LIMIT 1")
            self._audit_last_hash = (row or {}).get("hash") or ""
        return self._audit_last_hash

    def audit(self, actor: str, action: str, target: str, params: dict, result: str, ip: str = ""):
        """写审计日志。

        E4：params 中的 api_key/token/secret/账号自动脱敏；
        D4：写入 prev_hash/hash 构成链式哈希，任何篡改都可被 /audit/verify 检出。
        M5 审计身份：api_key_id 从鉴权中间件的 ContextVar 读取（主密钥="master"、
        子密钥=行 id、未鉴权=""→存 NULL），修复此前恒为 None 导致审计链身份缺失。
        """
        try:
            from gateway.masking import mask_dict
            safe_params = mask_dict(params)
        except Exception:  # noqa: BLE001
            safe_params = params
        try:
            from gateway.auth import current_api_key_id
            key_id = current_api_key_id.get("") or None
        except Exception:  # noqa: BLE001 gateway 未初始化（纯 DB 层单测等）
            key_id = None
        rec = {
            "actor": actor, "api_key_id": key_id, "action": action, "target": target,
            "params_json": json.dumps(safe_params, ensure_ascii=False, default=str),
            "result": result, "ip": ip, "created_at": now_iso(),
        }
        with _audit_lock:
            prev = self._last_audit_hash()
            h = audit_chain_hash(prev, rec)
            rec["prev_hash"] = prev
            rec["hash"] = h
            try:
                rid = self.insert("audit_log", rec)
                self._audit_last_hash = h
                return rid
            except sqlite3.Error:
                # 极端情况下（旧库缺列）退化为无链写入，保证审计不丢
                rec.pop("prev_hash", None)
                rec.pop("hash", None)
                return self.insert("audit_log", rec)

    def verify_audit_chain(self, limit: int = 200_000) -> dict:
        """D4：校验审计日志 hash 链完整性，定位第一处断链。"""
        cols = ", ".join(("id",) + _AUDIT_HASH_FIELDS + ("prev_hash", "hash"))
        rows = self.query(f"SELECT {cols} FROM audit_log ORDER BY id LIMIT ?", (limit,))
        prev = ""
        checked = 0
        legacy = 0
        broken: list[dict] = []
        for r in rows:
            stored = r.get("hash") or ""
            if not stored:
                legacy += 1          # D4 之前写入的历史记录，不参与链校验
                continue
            r_prev = r.get("prev_hash") or ""
            if checked == 0:
                prev = r_prev        # 起链锚点
            expect = audit_chain_hash(prev, r)
            if r_prev != prev:
                broken.append({"id": r["id"], "reason": "prev_hash 不连续（疑似记录被删除）",
                               "expect_prev": prev, "stored_prev": r_prev})
            elif expect != stored:
                broken.append({"id": r["id"], "reason": "内容被篡改（hash 不匹配）",
                               "expect": expect, "stored": stored})
            prev = stored
            checked += 1
        return {"ok": not broken, "total": len(rows), "checked": checked,
                "legacy": legacy, "broken_count": len(broken), "broken": broken[:20],
                "tail_hash": prev}


# 延迟初始化（由 app 生命周期持有）
_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        raise RuntimeError("DB not initialized")
    return _db


def init_db(path: Path) -> DB:
    global _db
    _db = DB(path)
    return _db
