"""SQLite 数据层：版本化迁移 + 最小 CRUD（§4.11 数据模型，工业级演进）。

- 建表采用**版本化迁移**：schema_migrations 记录已应用版本，新增表/字段只需追加迁移
  （旧库自动升级，无需手工删库）。
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("qmt_work.db")

_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    qmt_account_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT UNIQUE NOT NULL,
    user_id INTEGER,
    name TEXT DEFAULT '',
    scopes TEXT DEFAULT '',
    rate_limit INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
-- ⚠️ 孤儿表（阶段 5 Agent 预留）：sessions / messages / llm_config
-- 当前无任何后端代码读写（agent/ 后端已删），仅为阶段 5 重建 AgentCore 会话持久化预留。
-- 切勿删除，否则阶段 5 需重新建表；亦切勿在其它模块误用。
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT DEFAULT '',
    llm_config_snapshot TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    tool_calls_json TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    symbol TEXT,
    start TEXT,
    end TEXT,
    strategy TEXT,
    params_json TEXT,
    initial_capital REAL,
    metrics_json TEXT,
    trades_json TEXT,
    report_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    backtest_ids_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT DEFAULT 'global',
    params_json TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    api_key_id INTEGER,
    action TEXT,
    target TEXT,
    params_json TEXT,
    result TEXT,
    ip TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    dtype TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload_json TEXT,
    UNIQUE(code, dtype, ts)
);
CREATE TABLE IF NOT EXISTS account_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    ts TEXT NOT NULL,
    net_value REAL,
    positions_json TEXT,
    cash_json TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT UNIQUE NOT NULL,
    last_seq INTEGER DEFAULT 0,
    last_ts TEXT,
    status TEXT DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS backtest_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT,
    params_json TEXT,
    status TEXT,
    progress REAL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT DEFAULT 'global',
    provider TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    api_key_enc TEXT DEFAULT '',
    model TEXT DEFAULT '',
    temperature REAL DEFAULT 0.2,
    timeout_ms INTEGER DEFAULT 60000,
    is_default INTEGER DEFAULT 1,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS rebalance_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,
    direction TEXT,
    volume INTEGER,
    price REAL DEFAULT 0,
    status TEXT DEFAULT 'submitted',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS broker_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    broker_id TEXT DEFAULT '',
    client_path TEXT DEFAULT '',
    account_id TEXT DEFAULT '',
    account_type TEXT DEFAULT 'STOCK',
    session_id INTEGER DEFAULT 0,
    min_version TEXT DEFAULT '',
    active INTEGER DEFAULT 0,
    created_at TEXT
);
"""),
    (2, """
CREATE TABLE IF NOT EXISTS condition_orders (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    price_type TEXT DEFAULT 'limit',
    price REAL DEFAULT 0,
    volume INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    order_id TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    triggered_at TEXT
);
"""),
    (3, """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'webhook',
    enabled INTEGER DEFAULT 1,
    params_json TEXT DEFAULT '{}',
    events TEXT DEFAULT '*',
    template TEXT DEFAULT '{{title}}\n{{body}}',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER,
    event TEXT NOT NULL,
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    response TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""),
    (4, """
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT DEFAULT 'manual',
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL DEFAULT 0,
    volume INTEGER NOT NULL,
    price_type TEXT DEFAULT 'limit',
    remark TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    weights_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
"""),
    (5, """
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    event TEXT DEFAULT '*',
    metric TEXT DEFAULT '',
    op TEXT DEFAULT '>',
    threshold REAL DEFAULT 0,
    channel TEXT DEFAULT '*',
    cooldown_seconds INTEGER DEFAULT 300,
    last_triggered TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER,
    event TEXT,
    message TEXT,
    triggered_at TEXT NOT NULL
);
"""),
    (6, """
CREATE TABLE IF NOT EXISTS kline_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '1d',
    dt TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    fetched_at REAL DEFAULT 0,
    UNIQUE(code, period, dt)
);
CREATE INDEX IF NOT EXISTS idx_kline_cache_lookup ON kline_cache(code, period, dt);
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    url TEXT NOT NULL,
    events TEXT DEFAULT '*',
    secret TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    max_retries INTEGER DEFAULT 3,
    timeout_ms INTEGER DEFAULT 5000,
    headers_json TEXT DEFAULT '{}',
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    last_status TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    last_sent_at TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER,
    event TEXT,
    payload_json TEXT,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    http_status INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    delivered_at TEXT DEFAULT ''
);
"""),
    (7, """
CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
"""),
    (8, """
CREATE TABLE IF NOT EXISTS config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    action TEXT DEFAULT 'set',
    old_value TEXT DEFAULT '',
    new_value TEXT DEFAULT '',
    actor TEXT DEFAULT 'system',
    ip TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_history_key ON config_history(key);
CREATE INDEX IF NOT EXISTS idx_config_history_time ON config_history(created_at);
"""),
    (9, """
-- P1 高频查询索引补全（旧库升级自动应用；CREATE INDEX IF NOT EXISTS 幂等）
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_notification_log_created ON notification_log(created_at);
CREATE INDEX IF NOT EXISTS idx_notification_log_status ON notification_log(status);
CREATE INDEX IF NOT EXISTS idx_account_snapshot_ts ON account_snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_account_snapshot_account ON account_snapshot(account_id);
CREATE INDEX IF NOT EXISTS idx_backtest_jobs_status ON backtest_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_created ON webhook_deliveries(created_at);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_condition_orders_status ON condition_orders(status);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_code ON paper_orders(code);
CREATE INDEX IF NOT EXISTS idx_target_portfolios_status ON target_portfolios(status);
CREATE INDEX IF NOT EXISTS idx_market_cache_code ON market_cache(code, dtype);
"""),
    (10, """
-- P0 策略运行容器：在平台内把生成的策略当作实盘/模拟机器人运行
CREATE TABLE IF NOT EXISTS strategy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    strategy_type TEXT NOT NULL,
    codes_json TEXT DEFAULT '[]',
    params_json TEXT DEFAULT '{}',
    mode TEXT NOT NULL DEFAULT 'paper',
    conn_id TEXT DEFAULT '',
    account_id TEXT DEFAULT '',
    period TEXT DEFAULT '1d',
    interval_seconds REAL DEFAULT 60,
    volume INTEGER DEFAULT 100,
    max_positions INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'stopped',
    last_signal TEXT DEFAULT '',
    last_action TEXT DEFAULT '',
    last_eval_at TEXT DEFAULT '',
    held_volume REAL DEFAULT 0,
    pnl REAL DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_status ON strategy_runs(status);
CREATE TABLE IF NOT EXISTS strategy_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    signal TEXT DEFAULT '',
    action TEXT DEFAULT '',
    message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategy_logs_run ON strategy_logs(run_id, id);
"""),
    (11, """
-- 阶段 2.4：券商档案落库（热插拔 profile 持久化，重启不丢；内置档案仍来自代码）
CREATE TABLE IF NOT EXISTS broker_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    adapter TEXT NOT NULL DEFAULT 'xtp',
    profile_json TEXT NOT NULL DEFAULT '{}',
    is_custom INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_profiles_custom ON broker_profiles(is_custom);
"""),
]

# 表 -> 向后兼容扩展字段（幂等补列，TEXT DEFAULT ''）
_EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    # api_keys：IP 白名单 / 过期 / 轮换宽限
    "api_keys": ("ip_allow", "expires_at", "grace_until"),
    # audit_log：D4 hash 链防篡改
    "audit_log": ("prev_hash", "hash"),
    # condition_orders：A3 跨日续作与到期 + 阶段 2 拒单次日重试
    "condition_orders": ("valid_days", "expire_at", "last_check_date", "expired_at",
                         "retry_date", "retry_count"),
}
# 参与审计 hash 计算的字段（顺序固定，改动会使旧链失效）
_AUDIT_HASH_FIELDS = ("actor", "api_key_id", "action", "target",
                      "params_json", "result", "ip", "created_at")

_lock = threading.Lock()
_audit_lock = threading.Lock()


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
            with _lock:
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
                    with _lock:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {c} TEXT DEFAULT ''")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with _lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with _lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, data: dict) -> int:
        keys = list(data.keys())
        sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        with _lock:
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def upsert(self, table: str, data: dict) -> int:
        """INSERT OR REPLACE：依赖表上的 UNIQUE 约束（如 market_cache 的 code/dtype/ts）。

        重复爬取/重复 tick 时刷新数据而非报错。
        """
        keys = list(data.keys())
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        with _lock:
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
                with _lock:
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
        """
        try:
            from gateway.masking import mask_dict
            safe_params = mask_dict(params)
        except Exception:  # noqa: BLE001
            safe_params = params
        rec = {
            "actor": actor, "api_key_id": None, "action": action, "target": target,
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
