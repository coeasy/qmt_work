"""SQLite 版本化迁移与表结构扩展定义（自 app/db.py 拆出，2026-09 重构第三期）。

- ``MIGRATIONS``：版本化 DDL 列表 [(version, sql)]；``DB._migrate``（app/db.py）按版本
  升序执行、PRAGMA user_version 记账，语句均幂等（IF NOT EXISTS）。
- ``EXTRA_COLUMNS``：表 -> 向后兼容扩展字段；``DB._ensure_columns`` 幂等补 TEXT DEFAULT '' 列。

改动约束：已随版本发布的迁移 SQL 不可修改（只能追加新版本条目），
否则既有部署的 user_version 记账会跳过这些变更。
"""

MIGRATIONS: list[tuple[int, str]] = [
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
    client_mode TEXT DEFAULT 'auto',
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
    (12, """
-- 移除智能助手（Agent）：原会话/消息/LLM 配置表现已废弃，1 会话落盘不再使用。
-- 已迁移旧库（schema_migrations 已达 v11）不含本迁移产生的表，故 DROP 幂等安全；
-- 全新库也不再创建这三张表（v1 已移除建表语句）。
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS llm_config;
"""),
    (13, """
-- API Key 使用追踪：记录最近使用时间与累计调用次数，便于识别失效/残留在列表展示真实有效性。
ALTER TABLE api_keys ADD COLUMN last_used_at TEXT DEFAULT '';
ALTER TABLE api_keys ADD COLUMN use_count INTEGER DEFAULT 0;
"""),
    (14, """
-- 行情数据 热/归档 分离：
--   kline_cache   = 今年数据（热表，由同步任务定时更新）
--   kline_archive = 今年以前的历史数据（独立归档存储，降低热表体积、加速今年查询）
--   adjust       = 复权标记预留列（''=券商原始值，qfq/hfq 供后续支持前复权/后复权）
-- 把现有库中早于本年的历史 K 线迁移进归档表，保证热表仅剩今年数据、不丢失历史。
CREATE TABLE IF NOT EXISTS kline_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '1d',
    dt TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    fetched_at REAL DEFAULT 0,
    adjust TEXT DEFAULT '',
    UNIQUE(code, period, dt)
);
CREATE INDEX IF NOT EXISTS idx_kline_archive_lookup ON kline_archive(code, period, dt);
ALTER TABLE kline_cache ADD COLUMN adjust TEXT DEFAULT '';
INSERT OR REPLACE INTO kline_archive (code,period,dt,open,high,low,close,volume,amount,fetched_at)
SELECT code,period,dt,open,high,low,close,volume,amount,fetched_at
FROM kline_cache WHERE dt < (strftime('%Y','now') || '-01-01');
DELETE FROM kline_cache WHERE dt < (strftime('%Y','now') || '-01-01');
"""),
    (15, """
-- G3 资金流落库与回放：盘后/盘中定时采集个股资金流快照，支持历史回看。
-- net = outside - inside（主动买卖净额，外盘-内盘，真实口径，est=false）。
-- 同一 code 多次采集按 ts 追加（不 UNIQUE），回放按时间区间取序列。
CREATE TABLE IF NOT EXISTS moneyflow_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    ts TEXT NOT NULL,
    inside REAL,
    outside REAL,
    net REAL,
    volume_ratio REAL,
    source TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_moneyflow_cache_lookup ON moneyflow_cache(code, ts);
"""),
    (16, """
-- G1-4 本地数据仓（本地主源，供离线查询 / 全市场选股 / 降级兜底）：
-- 远程源失败时降级查询并标 stale=True（降级≠造假），G7 选股走本地向量化。
-- 与 kline_cache 的关系：kline_cache 是券商直连的**展示 TTL 缓存**（近年数据、
-- 复权未入唯一键）；local_bars 是**离线仓库**（复权维度入主键、可全市场增量同步、
-- 可配置磁盘策略），两条线解耦，避免改动热路径。
CREATE TABLE IF NOT EXISTS local_bars (
    code TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '1d',
    adjust TEXT NOT NULL DEFAULT '',
    dt TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    fetched_at TEXT DEFAULT '',
    PRIMARY KEY (code, period, adjust, dt)
);
CREATE INDEX IF NOT EXISTS idx_local_bars_lookup
    ON local_bars(code, period, adjust, dt);
CREATE TABLE IF NOT EXISTS local_stock_list (
    code TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    category TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS local_boards (
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    last REAL, change_pct REAL, amount REAL,
    updated_at TEXT DEFAULT '',
    PRIMARY KEY (kind, code)
);
CREATE TABLE IF NOT EXISTS local_sync_meta (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
"""),
    (17, """
-- M4 迁移账本收编：strategy_market 此前是 tools/strategy_market.py 里的
-- ad-hoc 惰性建表（账本外 schema，漂移无感知）。DDL 原样收编进迁移体系；
-- 代码里的 _ensure_table 保留为幂等兜底（覆盖跳过迁移的内存测试库）。
CREATE TABLE IF NOT EXISTS strategy_market (
    id TEXT PRIMARY KEY, title TEXT DEFAULT '', author TEXT DEFAULT '',
    description TEXT DEFAULT '', type TEXT DEFAULT '', content TEXT DEFAULT '',
    tags_json TEXT DEFAULT '[]', created_at TEXT, downloads INTEGER DEFAULT 0
);
"""),
    (18, """
-- M6 复权维度入唯一键：kline_cache/kline_archive 原 UNIQUE(code,period,dt)
-- 不含 adjust，qfq/hfq/原始价三份数据互相覆盖（INSERT OR REPLACE 互相挤掉），
-- 且 get_or_fetch 曾用 last_adjust 复用标记——把券商原始价错标成 qfq。
-- 重建两表：UNIQUE(code, period, adjust, dt)，三份数据各行其道；
-- 存量行原样迁移（历史行 adjust 本就是最后一次写入者的标记）。
ALTER TABLE kline_cache RENAME TO kline_cache_old;
CREATE TABLE kline_cache (
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
INSERT INTO kline_cache (code,period,dt,open,high,low,close,volume,amount,fetched_at,adjust)
    SELECT code,period,dt,open,high,low,close,volume,amount,fetched_at,adjust
    FROM kline_cache_old;
DROP TABLE kline_cache_old;
CREATE INDEX IF NOT EXISTS idx_kline_cache_lookup ON kline_cache(code, period, adjust, dt);
ALTER TABLE kline_archive RENAME TO kline_archive_old;
CREATE TABLE kline_archive (
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
INSERT INTO kline_archive (code,period,dt,open,high,low,close,volume,amount,fetched_at,adjust)
    SELECT code,period,dt,open,high,low,close,volume,amount,fetched_at,adjust
    FROM kline_archive_old;
DROP TABLE kline_archive_old;
CREATE INDEX IF NOT EXISTS idx_kline_archive_lookup ON kline_archive(code, period, adjust, dt);
"""),
    (19, """
-- Phase 2 数据溯源：每根本地 K 线必须能够追溯到 provider/batch，且保留
-- 质量状态与内容校验值。旧库用 ADD COLUMN 增量升级，不改变既有主键与数据。
ALTER TABLE local_bars ADD COLUMN provider_id TEXT DEFAULT '';
ALTER TABLE local_bars ADD COLUMN batch_id TEXT DEFAULT '';
ALTER TABLE local_bars ADD COLUMN checksum TEXT DEFAULT '';
ALTER TABLE local_bars ADD COLUMN schema_version TEXT DEFAULT '1';
ALTER TABLE local_bars ADD COLUMN quality_state TEXT DEFAULT 'unknown';
CREATE INDEX IF NOT EXISTS idx_local_bars_provenance
    ON local_bars(provider_id, batch_id, dt);
"""),
    (20, """
-- Phase 6 Durable JobRuntime：任务状态、租约、心跳和 checkpoint 持久化。
CREATE TABLE IF NOT EXISTS runtime_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    error TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until REAL,
    heartbeat_at REAL,
    checkpoint_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runtime_jobs_status ON runtime_jobs(status, priority, created_at);
"""),
    (21, """
-- Phase 5-7 data plane：交易所日历与可复现 Dataset Snapshot 元数据。
CREATE TABLE IF NOT EXISTS exchange_calendar (
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT 'regular',
    calendar_source TEXT NOT NULL DEFAULT '',
    calendar_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (market, exchange, trade_date, session)
);
CREATE INDEX IF NOT EXISTS idx_exchange_calendar_date
    ON exchange_calendar(exchange, trade_date);
CREATE TABLE IF NOT EXISTS dataset_snapshots (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL DEFAULT '',
    as_of TEXT NOT NULL DEFAULT '',
    coverage_start TEXT NOT NULL DEFAULT '',
    coverage_end TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',
    quality_state TEXT NOT NULL DEFAULT 'unknown',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_snapshot_version
    ON dataset_snapshots(dataset_id, version);
CREATE INDEX IF NOT EXISTS idx_dataset_snapshot_batch
    ON dataset_snapshots(provider_id, batch_id, created_at);
"""),
    (22, """
-- Phase 9 Plugin Kernel：仅保存声明式清单与状态，不保存插件可访问的原始 DB/密钥。
CREATE TABLE IF NOT EXISTS plugin_catalog (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'installed',
    checksum TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_plugin_catalog_state ON plugin_catalog(state);
"""),
]


# 表 -> 向后兼容扩展字段（幂等补列，TEXT DEFAULT ''）
EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    # api_keys：IP 白名单 / 过期 / 轮换宽限 / 使用追踪
    "api_keys": ("ip_allow", "expires_at", "grace_until", "last_used_at", "use_count"),
    # audit_log：D4 hash 链防篡改
    "audit_log": ("prev_hash", "hash"),
    # broker_connections：客户端模式（auto 自动推断 / mini 极速版 / full 完整版大客户端）
    "broker_connections": ("client_mode",),
    # condition_orders：A3 跨日续作与到期 + 阶段 2 拒单次日重试 + P1-5 盘中重试/终态核销
    "condition_orders": ("valid_days", "expire_at", "last_check_date", "expired_at",
                         "retry_date", "retry_count", "intraday_retry", "next_retry_at",
                         "settle_status"),
    # kline_cache：复权标记预留列（''=券商原始值）
    "kline_cache": ("adjust",),
}
