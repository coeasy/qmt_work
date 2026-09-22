/**
 * 共享类型定义。
 *
 * ★ 口径来源：逐文件核对 backend/app/routes/*.py 的实际返回结构，**不是**猜测。
 * 后端统一响应包裹为 { code, message, data }，http.ts 负责解包，业务层只见 data。
 * 零 mock 契约：任何字段在无法取到时一律允许缺失，绝不用假值填充。
 */

export interface Envelope<T> {
  code: number;
  message?: string;
  data: T;
}

/* ---------------- 行情 ---------------- */

export interface Quote {
  code: string;
  name?: string;
  price: number;
  pre_close?: number;
  open?: number;
  high?: number;
  low?: number;
  volume?: number;
  amount?: number;
  change?: number;
  change_pct?: number;
  /**
   * 买一 / 卖一**标量**。
   *
   * ★ 曾是 `number[]`，但后端 `xtquant_client/xtp/quotes.py` 的 `bid` / `ask` /
   * `bid_vol` / `ask_vol` 都是 `_lst(..., 0)` —— **标量**，五档数组在 `bids` /
   * `asks`。按数组索引读（`r.bid?.[0]`）恒为 undefined，于是五档与买一卖一
   * 永远显示 `--`，且不报错。
   */
  bid?: number;
  ask?: number;
  bid_vol?: number;
  ask_vol?: number;
  /** 五档盘口（买一~买五 / 卖一~卖五），后端已归一为定长 5 的数组。 */
  bids?: DepthLevel[];
  asks?: DepthLevel[];
  time?: string;
  source?: string;
  stale?: boolean;
  as_of?: string;
}

/** 盘口单档：价格 + 量（后端 bids/asks 的元素结构）。 */
export interface DepthLevel {
  price: number;
  volume: number;
}

export interface Bar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount?: number;
}

export interface Instrument {
  code: string;
  name: string;
  exchange?: string;
  category?: string;
}

export interface BoardInfo {
  code: string;
  name: string;
  change_pct?: number;
  leader?: string;
  amount?: number;
}

/** 分钟级主买/主卖力道点（eltdx 资金流 strength 序列）。 */
export interface MoneyFlowPoint {
  t: string | null;
  buy: number | null;
  sell: number | null;
}

export type Period = "1m" | "5m" | "15m" | "30m" | "60m" | "1d" | "1w" | "1M";

/* ---------------- 账户与交易 ---------------- */

export type Side = "buy" | "sell";
export type PriceType = "limit" | "market";
/**
 * 订单状态 —— **必须**与后端平台标准词表一致：
 * `backend/xtquant_client/order_status.py`（PENDING/PARTIAL/FILLED/CANCELLED/
 * REJECTED/UNKNOWN）。
 *
 * ★ 为什么改过：此前这里写 `part_filled` / `canceled` / `submitted`，而后端权威词表是
 * `partial` / `cancelled` / `pending`。券商原始串（含 submitted/part_filled 等别名）
 * 已由 `normalize_order_status` 在服务侧收敛成标准词，前端若按旧名匹配就**永远匹配不上**，
 * 状态列显示原始英文串、撤单按钮不出现——属于静默错配，不报错但一直错。
 * 任务/算法单/条件单的 `canceled` 是**另一套**（后端确实返回 canceled），不在此列。
 */
export type OrderStatus =
  | "pending"
  | "partial"
  | "filled"
  | "cancelled"
  | "rejected"
  | "unknown";

export interface Position {
  code: string;
  name?: string;
  volume: number;
  /**
   * 可用数量。★ 后端契约字段名是 avail（xtquant_client/xtp/account.py 输出
   * {"avail":…,"cost":…,"market_value":…}），不是 available。
   */
  avail?: number;
  /** 成本价。★ 后端契约字段名是 cost，不是 cost_price。 */
  cost?: number;
  price?: number;
  market_value?: number;
  profit?: number;
  profit_pct?: number;
}

export interface Order {
  order_id: string;
  code: string;
  name?: string;
  side: Side;
  price: number;
  volume: number;
  filled?: number;
  status: OrderStatus;
  price_type?: PriceType;
  time?: string;
  remark?: string;
  conn_id?: string;
}

export interface Deal {
  deal_id: string;
  order_id?: string;
  code: string;
  side: Side;
  price: number;
  volume: number;
  amount?: number;
  time?: string;
}

/**
 * GET /account/status 的真实返回（account.py:account_status）。
 * 注意：字段是 assets 而非 total_assets；且带 positions 数组。
 */
export interface AccountStatus {
  connected: boolean;
  assets?: number;
  cash?: number;
  position_count?: number;
  positions?: Position[];
}

/** GET /account/grid 的单账户行（account.py:_account_row）。 */
export interface AccountGridRow {
  conn_id: string;
  name: string;
  broker: string;
  broker_id: string;
  account_id: string;
  account_type: string;
  connected: boolean;
  assets: number;
  cash: number;
  market_value: number;
  /** 该账户持仓浮动盈亏合计；**null 表示无数据**（不是 0，前端须显示「—」） */
  profit: number | null;
  position_count: number;
  order_count: number;
  deal_count: number;
  /** 非空表示该账户取数失败的原因（未连接 / BrokerError） */
  error: string;
}

/** GET /account/grid 的跨账户持仓单元。 */
export interface AccountGridPosition {
  code: string;
  name: string;
  total_volume: number;
  total_market_value: number;
  /** 合计成本（成本×股数）；无成本数据为 null */
  total_cost: number | null;
  /** 最新价（同一标的跨账户同价，取任一非空值） */
  price: number | null;
  /** 合计浮动盈亏；null 表示无数据 */
  profit: number | null;
  /** 盈亏比（%） */
  profit_pct: number | null;
  accounts: Array<{
    conn_id: string;
    name: string;
    volume: number;
    market_value: number;
    price: number | null;
    profit: number | null;
  }>;
}

export interface AccountGrid {
  account_count: number;
  connected_count: number;
  total_assets: number;
  total_cash: number;
  total_market_value: number;
  /** 全部账户浮动盈亏合计；null 表示无数据 */
  total_profit: number | null;
  accounts: AccountGridRow[];
  positions: AccountGridPosition[];
  generated_at: string;
}

/** GET /account/aggregate（account.py:account_aggregate）。 */
export interface AccountAggregate {
  account_count: number;
  total_assets: number;
  total_cash: number;
  total_market_value: number;
  orders_count: number;
  deals_count: number;
  accounts: Array<{
    conn_id: string;
    name: string;
    broker: string;
    account_id?: string;
    account_type?: string;
    assets?: number;
    cash?: number;
    market_value?: number;
    position_count?: number;
    error?: string;
  }>;
  positions: Array<{ code: string; name: string; volume: number; market_value: number }>;
}

export interface NetValuePoint {
  ts: string;
  net_value: number;
}

export interface SlippageRow {
  time?: string;
  side?: Side;
  price?: number;
  slippage_open_bps?: number | null;
  slippage_close_bps?: number | null;
  slippage_avg_bps?: number | null;
}

export interface SlippageReport {
  code: string;
  samples: SlippageRow[];
  avg_abs_slippage_avg_bps: number;
}

/** 批量执行逐单结果（gateway/batch_execution.py）。 */
export interface BatchItemResult {
  conn_id?: string;
  code?: string;
  order_id?: string;
  ok?: boolean;
  status?: string;
  detail?: string;
  reason?: string;
}

export interface BatchOrderSummary {
  total: number;
  ok: number;
  failed?: number;
  batch_id?: string;
  results?: BatchItemResult[];
}

/* ---------------- 券商 ---------------- */

export interface BrokerProfile {
  id: string;
  name: string;
  adapter: string;
  supported_account_types?: string[];
  default_client_path?: string;
  planned?: boolean;
}

export interface BrokerConnection {
  conn_id: string;
  broker_id: string;
  broker_name?: string;
  account_id?: string;
  account_type?: string;
  active?: boolean;
  connected?: boolean;
  client_path?: string;
  /**
   * 后端判定：client_path 是否真实存在（manager.status_list）。
   * false = 指向不存在目录的历史/测试残留，永远连不上，界面须显式标记并可一键清理。
   * 未返回（旧后端）时为 undefined ⇒ 视为未知，不标记。
   */
  path_exists?: boolean;
  runtime_mode?: string;
}

/* ---------------- 算法与自动化 ---------------- */

export type AlgoKind = "twap" | "vwap" | "iceberg" | "pov";

/**
 * 算法单作业（engines/algo.py:submit 构造的 job）。
 * ★ 字段是 volume / algo / direction，不是 total_volume / algo_type / side。
 */
export interface AlgoOrder {
  algo_id: string;
  code: string;
  direction: Side;
  volume: number;
  algo: AlgoKind;
  duration: number;
  slices: number;
  price_type: PriceType;
  limit_price: number;
  remark: string;
  visible_pct: number;
  participation_rate: number;
  status: "pending" | "running" | "paused" | "done" | "canceled" | "failed";
  done: number;
  slices_done: number;
  error: string;
  created: string;
  conn_id?: string;
}

export type ConditionStatus = "pending" | "triggered" | "canceled" | "expired";

/**
 * 条件单（engines/condition_order.py:_view）。
 * ★ 主键字段是 id，不是 cid；触发价字段是 trigger_price，不是 trigger_value。
 */
export interface ConditionOrder {
  id: string;
  code: string;
  side: Side;
  trigger_type: "gte" | "lte";
  trigger_price: number;
  price_type: PriceType;
  price: number;
  volume: number;
  status: ConditionStatus;
  order_id: string;
  remark: string;
  created_at: string;
  triggered_at: string;
  valid_days: number;
  expire_at: string;
  last_check_date: string;
  expired_at: string;
  retry_count: number;
  retry_date: string;
  intraday_retry: number;
  next_retry_at: string;
  settle_status: string;
}

/** GET /trade/conditions 的返回（engines/condition_order.py:status）。 */
export interface ConditionStatusPayload {
  running: boolean;
  interval: number;
  total: number;
  pending: number;
  orders: ConditionOrder[];
}

/** 告警规则（alerts.py：DB 行，主键 id，enabled 为 0/1）。 */
export interface AlertRule {
  id: number;
  name: string;
  enabled: number;
  event: string;
  metric: string;
  op: string;
  threshold: number;
  channel: string;
  cooldown_seconds: number;
  created_at: string;
}

export interface AlertHistoryRow {
  id: number;
  [k: string]: unknown;
}

/** 出站 Webhook 订阅（webhooks.py:list_subs）。 */
export interface WebhookSub {
  id: number;
  name?: string;
  url: string;
  events?: string;
  enabled?: number;
  [k: string]: unknown;
}

export interface WebhookDelivery {
  id?: number;
  sid?: number;
  url?: string;
  event?: string;
  status?: number;
  ok?: boolean;
  error?: string;
  ts?: string;
  [k: string]: unknown;
}

/** 涨停监控状态（engines/limitup.py:status）。 */
export interface LimitUpStatus {
  running: boolean;
  interval: number;
  limit_pct: number;
  cutoff: string;
  min_rise: number;
  buy_volume: number;
  do_trade: boolean;
  pool: Array<{ code: string; name: string }>;
  total_triggered: number;
  events: Array<Record<string, unknown>>;
}

/* ---------------- 系统 ---------------- */

/* 健康检查 / 能力清单的响应类型定义在 services/api/system.ts
   （它们与具体端点绑定，放在 API 层避免类型漂移）。 */

/** 任务运行时（app/runtime/jobs.py）——注意字段是 id 与 kind。 */
export interface RuntimeJob {
  id: string;
  kind: string;
  name?: string;
  status: "queued" | "running" | "done" | "failed" | "canceled";
  priority?: number;
  progress?: number;
  message?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  result?: unknown;
}

export interface RuntimeJobList {
  items: RuntimeJob[];
  count: number;
}

/** 调度（app/runtime/schedules.py）——字段是 id / kind。
 *
 * ⚠️ 时间字段名是 **next_run_at / last_run_at**（后端连列名一起返回，
 * 见 ``schedules.py`` 的 ``_decorate``）。此前这里写的是 ``next_run/last_run``，
 * 界面于是**永远读到 undefined**、两列恒显示 `--` —— 端点 200、契约测试也抓不到，
 * 因为它从不校验字段名。改字段名必须同时改 RuntimeJobs 的两列渲染。
 */
export interface ScheduleItem {
  id: string;
  kind: string;
  cron: string;
  name: string;
  enabled: boolean;
  misfire_policy: string;
  params?: Record<string, unknown>;
  next_run_at?: string;
  last_run_at?: string;
  last_status?: string;
}

export interface ScheduleList {
  items: ScheduleItem[];
  count: number;
}

/** API Key（apikeys.py：主键 id，返回 key_prefix 而非明文）。 */
export interface ApiKeyItem {
  id: number;
  name: string;
  scopes: string;
  rate_limit: number;
  status: string;
  created_at: string;
  ip_allow: string;
  expires_at: string;
  grace_until: string;
  last_used_at: string;
  use_count: number;
  key_prefix: string;
}

export interface ApiKeyCreated {
  id: number;
  api_key: string;
  name: string;
  scopes: string;
  rate_limit: number;
  ip_allow: string;
  expires_at: string;
}

/** 审计日志行（audit.py，含 D4 hash 链字段）。 */
export interface AuditRow {
  id: number;
  actor: string;
  action: string;
  target: string;
  params_json?: string;
  result: string;
  ip?: string;
  created_at: string;
  prev_hash?: string;
  hash?: string;
}

export interface AuditVerifyResult {
  ok: boolean;
  broken_count: number;
  broken: Array<{ id: number; [k: string]: unknown }>;
  [k: string]: unknown;
}

/** WAL 统计（reconcile.py:wal_stats）。 */
export interface WalStats {
  path: string;
  size: number;
  snapshot_size: number;
  checkpoint_threshold: number;
  records: number;
  by_entity: Record<string, number>;
}

/** 运行时引擎参数（config.py，键为参数名）。 */
export type RuntimeConfig = Record<
  string,
  { value?: unknown; default?: unknown; desc?: string; [k: string]: unknown }
>;

export interface ConfigHistoryRow {
  id: number;
  key?: string;
  old_value?: unknown;
  new_value?: unknown;
  created_at?: string;
  [k: string]: unknown;
}

export interface RiskDailyStats {
  orders_today?: number;
  amount_today?: number;
  circuit_tripped?: boolean;
  circuit_reason?: string;
  [k: string]: unknown;
}

export interface RiskConfig {
  max_amount?: number;
  min_qty?: number;
  max_position_ratio?: number;
  max_single_position_ratio?: number;
  max_orders_per_min?: number;
  daily_amount_limit?: number;
  daily_loss_limit?: number;
  per_code_daily_orders?: number;
  daily?: RiskDailyStats;
  [k: string]: unknown;
}

/* ---------------- WS 事件 ---------------- */

export type WsEventType =
  | "snapshot"
  | "quotes"
  | "order"
  | "deal"
  | "account"
  | "risk.blocked"
  | "signal_live"
  | "signal_pending"
  | "signal_dry_run"
  | "algo"
  | "limitup"
  | "condition_created"
  | "alert"
  | "heartbeat";

export interface WsMessage<T = unknown> {
  type: WsEventType | string;
  data: T;
  seq?: number;
  /**
   * 全量快照帧（`type === "snapshot"`）携带的行情表 `{ CODE: Quote }`。
   * ⚠️ 与 `data` **互斥**：快照帧用 `quotes` 键，增量帧（`type === "quotes"`）
   * 才用 `data.items`。契约见 backend/sync/__init__.py 的
   * `send_full_snapshot` 与 `broadcast`。
   */
  quotes?: Record<string, Quote>;
}
