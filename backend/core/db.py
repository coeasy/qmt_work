"""SQLite 数据层：版本化迁移 + 最小 CRUD（§4.11 数据模型，工业级演进）。

- 建表采用**版本化迁移**：schema_migrations 记录已应用版本，新增表/字段只需追加迁移
  （旧库自动升级，无需手工删库）。
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# ``now_iso`` 的唯一实现在 ``core.clock``（V11 R8 收敛）。此处**保留同名 re-export**：
# 存量代码大量 ``from core.db import now_iso``，删除会引发无谓的大范围改动；
# 但**不要再在本文件重新定义**（护栏 ``tests/test_clock_unity.py`` 会红）。
from core.clock import now_iso  # noqa: F401

log = logging.getLogger("qmt_work.db")

# 迁移与扩展列定义拆至 app/db_migrations.py（2026-09 第三期）；
# 模块级名字保持 _MIGRATIONS/_EXTRA_COLUMNS，既有 monkeypatch（tests/test_unit.py）不受影响。
from core.db_migrations import EXTRA_COLUMNS as _EXTRA_COLUMNS  # noqa: E402
from core.db_migrations import MIGRATIONS as _MIGRATIONS  # noqa: E402

# 参与审计 hash 计算的字段（顺序固定，改动会使旧链失效）
_AUDIT_HASH_FIELDS = ("actor", "api_key_id", "action", "target",
                      "params_json", "result", "ip", "created_at")

_lock = threading.Lock()
_audit_lock = threading.Lock()


class _RWLock:
    """读写锁（M3）：读共享、写独占。

    背景：原实现所有读写共用一把 threading.Lock —— WAL 虽让 SQLite 层面
    读写可并发，但应用层互斥把读也串行化了，长事务（行情微批写盘/备份）
    期间所有查询排队。现拆为读写锁：查询走 read（共享），写/迁移/备份走
    write（独占）。

    公平性（2026-09-14 调整）：**读者优先**——read() 不因「有写者排队」而让路。
    原先的「写优先防饥饿」在持续写负载下会把读者饿死（EOD 全市场落库实测
    7175 次写事务，期间读 DB 的 /trade/positions 由 0.019s 恶化到 4.73s）。
    HTTP 读延迟是用户可见的，优先级高于后台批处理吞吐。
    反向风险（写者被持续读者饿住）经实测不成立：EOD 在持续请求下正常推进并
    完成；且读都是短查询，最后一个读者退出时会 notify_all() 唤醒写者。

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
        # 读者**不等待写者**：读走独立只读连接（见 query），WAL 下与写（主连接）
        # 天然并发，本就不需要与写互斥。
        #
        # 此前这里是 `while self._writer or self._writers_waiting`（「写优先防写饥饿」），
        # 代价是**持续写负载下读者被饿死**：EOD 全市场落库（每只一次写事务，实测
        # 7175 次）期间，读 DB 的 /trade/positions 由 0.019s 恶化到 4.73s、
        # /capabilities 由 0.032s 到 1.97s，而同一时刻不读 DB 的 `/` 仅 0.007s；
        # 取消 EOD 后立刻全部恢复。HTTP 请求（读）的延迟是用户可见的，优先级高于
        # 后台批处理的吞吐，故读者不再让路。
        # 写者仍等待读者（见 write）：读都是短查询，且 query 的「回退主连接」分支
        # 本身走 write()，与写互斥的安全性不受影响。
        with self._cond:
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write(self, timeout: float | None = None):
        """获取写锁。``timeout`` 为 None 时无限等待（默认，保持既有语义）；
        给定秒数时超时抛 :class:`TimeoutError`。

        ★ 2026-09-29 补：停机路径必须用有界等待。此前的 `write()` 只有
        `self._cond.wait()`（无超时）——若某后台线程正持写锁做慢操作（大事务、
        EOD 落库、备份），`DB.close()` 会**永久卡住**，表现为「进程关不掉」
        （TD-25 同族：无界等待 = 可冻死进程）。``close()`` 现改走有界获取。
        """
        # `_writers_waiting` 仅保留为观测字段（写者排队数）：自 read() 改为
        # 「读者不让路」后，它不再参与读者调度——此前读者一见有写者排队就让路，
        # 在持续写负载下会把读者饿死（详见 read() 注释）。
        acquired = False
        with self._cond:
            self._writers_waiting += 1
            if timeout is None:
                while self._writer or self._readers:
                    self._cond.wait()
                acquired = True
            else:
                deadline = time.monotonic() + max(0.0, float(timeout))
                while self._writer or self._readers:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        break
                    self._cond.wait(remain)
                acquired = not (self._writer or self._readers)
            self._writers_waiting -= 1
            if acquired:
                self._writer = True
        if not acquired:
            raise TimeoutError(
                f"获取数据库写锁超时（{timeout}s）：可能有长写事务或读者未释放")
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


_rw = _RWLock(concurrent_reads=sqlite3.threadsafety >= 3)


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

    #: 备份分块拷贝的页数（默认 4 KiB/页 ⇒ 每个 progress 间隔约 16 MB）。
    #: 分块**只为让 `progress` 有机会按墙钟中止**「在推进但极慢」的备份；
    #: 它救不了「源库被锁死」—— 那种情况 CPython 不回调 progress、只无限重试，
    #: 真正的保护是 `backup_to()` 不再持有进程级写锁（见其 docstring）。
    _BACKUP_CHUNK_PAGES = 4096

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
        self._closed = False
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

    def close(self) -> bool:
        """P0-10：显式关闭数据库（停机逆序的最后一步）。

        此前完全依赖 GC 隐式回收：句柄与未 checkpoint 的 -wal 文件可能残留，
        打包客户端 / 频繁重启场景下会留下脏数据或文件锁。现显式完成：
        1) ``wal_checkpoint(TRUNCATE)`` —— 把 WAL 合并回主库并清零 -wal 文件；
        2) 依次关闭只读连接与主写连接。

        幂等：已关闭时重复调用返回 False，不抛异常。
        """
        if self._closed:
            return False
        self._closed = True
        # 停机路径上的锁必须有界：无界等待会让进程「关不掉」（TD-25 同族）。
        # checkpoint 只是优化——拿不到锁/超时就跳过，WAL 会在下次打开时自愈，
        # 数据本身已落盘（-wal 文件仍在，不会丢）。
        _close_to = float(os.environ.get("QMT_DB_CLOSE_TIMEOUT", "5"))
        try:
            with _rw.write(timeout=_close_to):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except TimeoutError as exc:
            log.warning("db close：%s —— 跳过 checkpoint（WAL 将于下次打开时恢复）", exc)
        except sqlite3.Error as exc:
            log.warning("db close：wal_checkpoint 失败（已忽略）：%s", exc)
        ro = self._ro
        if ro is not None and ro is not False:
            try:
                ro.close()
            except sqlite3.Error as exc:
                log.debug("db close：只读连接关闭异常（已忽略）：%s", exc)
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            log.debug("db close：主连接关闭异常（已忽略）：%s", exc)
        return True

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

    def ping(self) -> bool:
        """探活：能在这条连接上完成一次最简读取即视为可用。

        专供 ``GET /ready`` 就绪探针使用。之所以做成 DB 的方法而不是让路由写
        ``ctx.db.query("SELECT 1")``：路由层不得出现裸 SQL（门禁
        ``scripts/check_execution_architecture.py`` 会拦），且「什么算活着」
        属于 DB 自身的语义，应由 DB 承担。

        返回 ``False`` 而不是抛异常 —— 探针的调用方要的是**布尔就绪信号**，
        不是异常栈；把 ``sqlite3.Error`` 抛给路由只会让 /ready 变成 500，
        而它本该在 DB 坏掉时返回 503「未就绪」。
        """
        try:
            self.query("SELECT 1")
        except Exception:  # noqa: BLE001 — 任何读取失败都等价于「不可用」
            return False
        return True

    @contextlib.contextmanager
    def readonly_conn(self):
        """独占只读连接（**大结果集批处理**专用）：不占读写锁、与写者互不阻塞。

        与 ``query()`` 的分工（两者都合法，按结果集大小选）：
        - ``query()`` 复用共享只读连接并持 ``_rw.read()``，适合**小结果集**；
          但 ``write()`` 会等待 ``_readers == 0``，故大结果集取数期间会把写者
          堵住整个取数时长。
        - 本方法为**分块大结果集**而设：开一条**独立只读连接**（WAL 下与写天然
          并发）。既不阻塞写者，也不与写者争抢主连接的互斥量——后者是真实痛点：
          实测全市场批量读（3.2 万行小样）在并发写负载下由 **201ms 劣化到 3925ms
          （19.5×）**，20s 内只完成 3 轮；根因就是它与 9.7 万次写共用一条主连接。
          反过来，批量读也会把单次写入的 p99 从 0.15ms 推到 73.6ms。

        用法：在 ``with`` 块内 ``execute`` 并**流式**消费游标（**勿** ``fetchall``
        成列表——不物化整批行正是本方法要拿到的收益）。句柄生命周期 = 整个 with 块，
        故一次批处理应只开一次（在分块循环**外**），而不是每块开一次。

        内存库（``:memory:``）无法再开一条连接（新连接看到的是空库）→ 自动降级为
        主连接 + 写锁；此时与写互斥，语义安全（仅测试场景命中）。
        """
        if str(self.path) == ":memory:":
            with _rw.write():
                yield self._conn
            return
        conn = None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                   timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            conn = None
        if conn is None:
            # 只读介质 / URI 不受支持等 → 回退主连接 + 写锁（与 query() 同口径）
            with _rw.write():
                yield self._conn
            return
        try:
            yield conn
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

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

    # ---------------- 时间维度保留策略（防 account_snapshot / moneyflow_cache 无界增长）----
    def _prune_before(self, table: str, keep_days: int) -> int:
        days = int(keep_days)
        if days < 1:
            raise ValueError("keep_days 必须 >= 1")
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        # 有界获取：本方法跑在 ``asyncio.to_thread`` 的默认线程池里，而该池在解释器
        # 退出时会被 atexit join —— 无界等待 = 可能把进程钉死（TD-25 同族）。
        # 保留策略本就是尽力而为，拿不到锁就跳过本轮（下个周期再来）。
        with _rw.write(timeout=float(os.environ.get("QMT_PRUNE_LOCK_TIMEOUT", "30"))):
            cur = self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def prune_account_snapshot(self, keep_days: int = 90) -> int:
        """按时间保留账户净值快照，防止无界增长。

        快照循环每 5s 写一行/账户（非交易时段 60s 探活），单账户一年可达百万行、
        每行还带 ``positions_json``/``cash_json`` 两份 JSON ⇒ 数 GB 级冗余。
        而净值曲线读取只取最近若干点（``app/services/account_store.pnl_series``），
        旧行不再被任何路径消费。默认保留 90 天。
        """
        return self._prune_before("account_snapshot", keep_days)

    def prune_moneyflow_cache(self, keep_days: int = 30) -> int:
        """按时间保留资金流快照，防止无界增长。

        采集循环交易时段每 5 分钟写一批 ``snapshot_codes``；回放读取
        （``moneyflow_replay``）最多取 5000 条/代码，更早的行不被消费。默认保留 30 天。
        """
        return self._prune_before("moneyflow_cache", keep_days)

    async def aprune_account_snapshot(self, keep_days: int = 90) -> int:
        return await asyncio.to_thread(self.prune_account_snapshot, keep_days)

    async def aprune_moneyflow_cache(self, keep_days: int = 30) -> int:
        return await asyncio.to_thread(self.prune_moneyflow_cache, keep_days)

    # ---------------- 阶段 3：一致性备份（sqlite3 backup API） ----------------
    def backup_to(self, dst: Path, *, deadline_s: float = 300.0) -> bool:
        """用 sqlite3 backup API 生成**一致性**备份（含 WAL 中已提交但未 checkpoint 的数据）。

        相比逐文件 copy 主库 + -wal/-shm（可能拿到中间状态、且重启时 -wal 失效），
        `Connection.backup()` 在事务层面拷贝出单一完整文件，可直接单独使用/还原。

        ★★ 2026-09-28（R31 第 31 轮，实测「启动备份把整个应用冻死」后重写）两处修正：

        1. **不再持有进程级写锁 `_rw.write()`。** 旧实现是
           ``with _rw.write(): self._conn.backup(dst_conn)``，理由是「备份期间持锁，
           避免写入并发导致快照不一致」。但 CPython 的 ``Connection.backup()`` 一旦
           源库被任何其它连接持锁，就按 ``SQLITE_BUSY`` **每 0.25 s 无限重试、且不回调
           ``progress``**，调用方永远拿不回控制权。于是「一次卡住的备份」= **全进程
           所有读写一起排队**。实测（py-spy dump 真实进程）：7 个线程中 6 个卡在
           ``_rw.write()``，其中包含 lifespan 主协程（经 jobs reaper 的 ``upsert``）
           ⇒ ``TestClient.__enter__`` 永不返回 ⇒ **应用永远起不来**。
           一致性并不依赖这把**进程级**锁：SQLite 备份 API 自身就保证快照一致（源被
           改动时自动更新/重启）。去掉它只损失「拷贝期间进程内完全无写入」这一不必要的
           强约束，换来「卡住的备份只卡它自己」。

        2. **有界 + 预检磁盘。** 分块拷贝（``pages``）配合 ``progress`` 回调按墙钟中止
           （默认 300 s，**尽力而为**：受 CPython 实现粒度限制，且源库被锁死时 progress
           根本不会被回调 —— 那种情况只能靠「不持进程锁 + 启动不等结果」保命）；
           目标盘可用空间不足 ``1.5 × 库体积 + 64 MB`` 时直接放弃（**仅对 ≥64 MB 的
           大库生效**）—— 实测 585 MB 主库 × ``keep=10`` 把 98% 满的盘又写进去 4.4 GB。
           中止/失败时**清掉半份 ``.tmp``**，不再留下孤儿残片（历史遗留：
           ``data/backups/`` 里 8/30、9/11、9/12、9/25 各有一份 0 字节或半截的
           ``.db.tmp``，正是旧实现无界阻塞的化石）。
        """
        tmp = ""
        try:
            dst = Path(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(dst) + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
            need = self.file_size()
            # 只对**大库**做磁盘预检：小库（<64 MB）所需余量远小于任何正常卷的
            # 剩余空间，硬套 64 MB 安全垫反而会在紧张的 CI 盘上误判成「空间不足」。
            if need >= (64 << 20):
                free = shutil.disk_usage(str(dst.parent)).free
                if free < need + need // 2 + (64 << 20):
                    log.warning(
                        "跳过数据库备份：目标盘可用 %.2f GB 不足（源库 %.2f GB）",
                        free / 2 ** 30, need / 2 ** 30)
                    return False
            deadline = time.monotonic() + max(1.0, float(deadline_s))
            aborted: list[str] = []

            def _progress(_status: int, _remaining: int, _total: int) -> int:
                if time.monotonic() > deadline:
                    aborted.append("deadline")
                    return 1     # 非零 ⇒ 请求 SQLite 中止本次备份
                return 0

            dst_conn = sqlite3.connect(tmp)
            done = False
            try:
                try:
                    self._conn.backup(dst_conn, pages=self._BACKUP_CHUNK_PAGES,
                                      progress=_progress)
                    done = True
                except sqlite3.Error:
                    if not aborted:
                        raise
                if done and not aborted:
                    dst_conn.commit()
            finally:
                dst_conn.close()
            if aborted or not done:
                log.warning("数据库备份超时中止（>%.0fs），已放弃本次备份", deadline_s)
                return False
            os.replace(tmp, dst)   # 原子落位：进程崩溃不会留下半份备份
            return True
        except (sqlite3.Error, OSError) as exc:
            log.warning("sqlite backup API 失败：%s", exc)
            return False
        finally:
            # 未走到 os.replace 就清掉半份 .tmp：别在备份目录里留孤儿残片
            if tmp and os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.remove(tmp)

    def file_size(self) -> int:
        """主库文件字节数（取不到返回 0）。供备份的磁盘预检使用。"""
        try:
            return int(os.path.getsize(str(self.path)))
        except OSError:
            return 0

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
            from core.masking import mask_dict
            safe_params = mask_dict(params)
        except Exception:  # noqa: BLE001
            safe_params = params
        try:
            from core.auth_identity import current_api_key_id
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
