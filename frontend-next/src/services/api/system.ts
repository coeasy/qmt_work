import { http } from "../http";
import type {
  ApiKeyCreated,
  ApiKeyItem,
  AuditRow,
  AuditVerifyResult,
  ConfigHistoryRow,
  RuntimeConfig,
  RiskConfig,
  RiskDailyStats,
  RuntimeJob,
  RuntimeJobList,
  ScheduleItem,
  ScheduleList,
} from "@/shared/types";

/* ---------------- 类型（口径来源：app/routes/{health,capabilities,config,runtime}.py） ---------------- */

/** 通知渠道配置（notifications.py:list_configs）。后端为宽松字典，前端按索引签名读取。 */
export interface NotificationConfig {
  id: number;
  name?: string;
  type?: string;
  channel?: string;
  template?: string;
  events?: string;
  enabled?: number | boolean;
  [k: string]: unknown;
}

/** 通知配置创建/更新载荷（notifications.py:save_notification）。带 id 即更新。 */
export interface NotificationPayload {
  id?: number;
  name?: string;
  type?: string;
  channel?: string;
  template?: string;
  events?: string;
  enabled?: number | boolean;
  [k: string]: unknown;
}

export interface HealthCheck {
  name: string;
  /** pass / warn / fail */
  status: string;
}

export interface HealthResponse {
  status: "pass" | "fail";
  service: string;
  version: string;
  uptime_seconds: number;
  db: boolean;
  brokers: Array<{ conn_id: string; broker: string; connected: boolean; active: boolean }>;
  engines: { backtest_queue: boolean; limitup: boolean; algo: boolean; ws: boolean };
  trading_session: { mode: string; active: boolean | null };
  checks: HealthCheck[];
  lifecycle: { ready: boolean; stopping: boolean; phases: Record<string, string> };
}

export interface LiveResponse {
  status: string;
  service: string;
  version: string;
  uptime_seconds: number;
  checks: HealthCheck[];
}

export interface ReadyResponse {
  ready: boolean;
  version: string;
  uptime_seconds: number;
  db: boolean;
  started: boolean;
  engines: { ws: boolean; backtest: boolean; sync: boolean };
  required_phases: Record<string, string>;
  lifecycle: { ready: boolean; stopping: boolean; phases: Record<string, string> };
}

/** 能力注册表条目（capabilities.py:build_capabilities）。 */
export interface CapabilityItem {
  id: string;
  path: string;
  method: string;
  category: string;
  summary?: string;
  risk_level?: string;
  confirm_required?: boolean;
  agent_visible?: boolean;
  tool_name?: string;
  param_schema?: unknown;
}

export interface CapabilitiesResponse {
  total: number;
  agent_visible: number;
  items: CapabilityItem[];
}

export interface CapabilitiesSummary {
  rest_total: number;
  agent_visible_total: number;
  by_category: Record<string, { total: number; agent_visible: number; write: number; read: number }>;
}

/** 运行时作业提交载荷（runtime.py:runtime_jobs_submit）。 */
export interface RuntimeJobSubmit {
  /** system.* | sync | screen */
  kind: string;
  name?: string;
  params?: Record<string, unknown>;
  priority?: number;
}

/** 调度创建载荷（runtime.py:schedules_create）。 */
export interface ScheduleCreate {
  kind: string;
  /** 标准 cron 表达式 */
  cron: string;
  name?: string;
  /** coalesce | skip | catchup */
  misfire_policy?: string;
  enabled?: boolean;
  params?: Record<string, unknown>;
}

/**
 * 系统域 API。路径与 app/routes/{health,capabilities,config,apikeys,audit,runtime}.py 对应。
 *
 * ★ 契约修正（相对初版）：
 *   - /capabilities 返回 {total, agent_visible, items}，**不是**裸数组
 *   - /runtime/jobs 与 /runtime/schedules 返回 {items, count}，**不是**裸数组
 *   - /runtime/schedules 的创建字段是 kind（不是 job_kind）
 *   - /config/runtime/history 返回 {rows}；rollback 的 body 是 {id}（不是 {version}）
 *   - /api-keys 的主键是 id（int），列表返回 key_prefix 而非明文
 */
export const systemApi = {
  live: () => http.get<LiveResponse>("/live"),

  health: () => http.get<HealthResponse>("/health"),

  ready: () => http.get<ReadyResponse>("/ready"),

  capabilities: (category = "", method = "") =>
    http.get<CapabilitiesResponse>("/capabilities", { query: { category, method } }),

  capabilitiesSummary: () => http.get<CapabilitiesSummary>("/capabilities/summary"),

  platformStatus: () => http.get<Record<string, unknown>>("/platform/status"),

  /* ---- 运行时引擎参数（热更新） ---- */

  config: () => http.get<RuntimeConfig>("/config/runtime"),

  updateConfig: (patch: Record<string, unknown>) =>
    http.put<{ saved: boolean; changed: string[]; config: RuntimeConfig }>(
      "/config/runtime",
      patch,
    ),

  resetConfig: (key = "") =>
    http.post<{ reset: string[]; config: RuntimeConfig }>("/config/runtime/reset", { key }),

  configHistory: (limit = 50) =>
    http.get<{ rows: ConfigHistoryRow[] }>("/config/runtime/history", { query: { limit } }),

  /** ★ body 键是 id（历史记录 id），不是 version */
  rollbackConfig: (id: number) =>
    http.post<{ rolled_back: number; config: RuntimeConfig }>("/config/runtime/rollback", { id }),

  /* ---- 风控参数 ---- */

  riskConfig: () => http.get<RiskConfig>("/config/risk"),

  updateRiskConfig: (patch: Record<string, unknown>) =>
    http.put<{ saved: boolean; changed: string[]; config: RiskConfig }>("/config/risk", patch),

  riskDaily: () => http.get<RiskDailyStats>("/config/risk/daily"),

  /** 熔断开关：trip = 手动熔断（暂停买入开仓）/ reset = 解除 */
  riskCircuit: (action: "trip" | "reset", reason = "") =>
    http.post<RiskDailyStats>("/config/risk/circuit", { action, reason }),

  /* ---- API Key ---- */

  apiKeys: () => http.get<ApiKeyItem[]>("/api-keys"),

  createApiKey: (body: {
    name?: string;
    scopes?: string;
    rate_limit?: number;
    ip_allow?: string;
    expires_at?: string;
  }) => http.post<ApiKeyCreated>("/api-keys", body),

  updateApiKey: (
    kid: number,
    patch: Partial<{ name: string; scopes: string; rate_limit: number; status: string; ip_allow: string; expires_at: string }>,
  ) => http.patch<{ updated: boolean }>(`/api-keys/${kid}`, patch),

  deleteApiKey: (kid: number) => http.del<{ deleted: boolean }>(`/api-keys/${kid}`),

  batchDeleteApiKeys: (ids: number[]) =>
    http.post<{ deleted: number }>("/api-keys/batch-delete", { ids }),

  /** 轮换：新密钥立即生效，旧密钥立即失效；返回一次性明文 api_key */
  rotateApiKey: (kid: number) =>
    http.post<{ id: number; api_key: string; grace_until: string; note: string }>(
      `/api-keys/${kid}/rotate`,
    ),

  cleanUnusedApiKeys: (days = 30) =>
    http.post<{ deleted: number; cutoff: string }>("/api-keys/clean-unused", { days }),

  /* ---- 审计 ---- */

  audit: (query?: { limit?: number; action?: string }) =>
    http.get<AuditRow[]>("/audit", { query }),

  auditVerify: (limit = 200_000) =>
    http.get<AuditVerifyResult>("/audit/verify", { query: { limit } }),

  /* ---- 通知渠道 ---- */

  notifications: () => http.get<NotificationConfig[]>("/notifications"),

  /** 带 id 即为更新（后端按 id 走 UPDATE） */
  saveNotification: (body: NotificationPayload) =>
    http.post<{ id: number }>("/notifications", body),

  deleteNotification: (nid: number) => http.del<{ deleted: boolean }>(`/notifications/${nid}`),

  /** ★ 批量删除的 body 键是 ids（与 alerts / webhooks / api-keys 一致） */
  batchDeleteNotifications: (ids: number[]) =>
    http.post<{ deleted: number }>("/notifications/batch-delete", { ids }),

  notificationLogs: (limit = 50) =>
    http.get<unknown[]>("/notifications/logs", { query: { limit } }),

  testNotification: (body: Record<string, unknown>) =>
    http.post<{ sent: boolean; preview?: unknown }>("/notifications/test", body),

  /* ---- 任务运行时 ---- */

  /** ★ 返回 {items, count} 包裹对象 */
  jobs: () => http.get<RuntimeJobList>("/runtime/jobs"),

  submitJob: (body: RuntimeJobSubmit) =>
    http.post<{ id: string; status: string }>("/runtime/jobs", body),

  job: (jobId: string) => http.get<RuntimeJob>(`/runtime/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    http.post<{ id: string; status: string }>(`/runtime/jobs/${jobId}/cancel`),

  /** ★ 返回 {items, count} 包裹对象 */
  schedules: (enabledOnly = false) =>
    http.get<ScheduleList>("/runtime/schedules", { query: { enabled_only: enabledOnly } }),

  /** ★ 字段是 kind，不是 job_kind */
  createSchedule: (body: ScheduleCreate) => http.post<ScheduleItem>("/runtime/schedules", body),

  updateSchedule: (id: string, body: Partial<ScheduleCreate>) =>
    http.put<ScheduleItem>(`/runtime/schedules/${id}`, body),

  deleteSchedule: (id: string) =>
    http.del<{ id: string; deleted: boolean }>(`/runtime/schedules/${id}`),

  triggerSchedule: (id: string) =>
    http.post<{ schedule_id: string; job_id: string }>(`/runtime/schedules/${id}/trigger`),
};

/** 参考数据 API。路径与 app/routes/reference.py 对应。 */
export const referenceApi = {
  calendar: (start?: string, end?: string) =>
    http.get<unknown[]>("/reference/calendar", { query: { start, end } }),

  sectors: () => http.get<unknown[]>("/reference/sectors"),

  sectorStocks: (code: string) =>
    http.get<unknown[]>("/reference/sector-stocks", { query: { code } }),

  financial: (code: string) =>
    http.get<Record<string, unknown>>("/reference/financial", { query: { code } }),
};

/* ---------------- 选股（app/routes/screen.py + app/screener/engine.py） ---------------- */

/** 选股结果行（evaluate_scan 产出的行结构）。 */
export interface ScreenRow {
  code: string;
  name?: string;
  close?: number;
  change_pct?: number;
  score?: number;
  [k: string]: unknown;
}

/** 选股响应（scan_async 返回体）。 */
export interface ScreenResponse {
  count: number;
  total_scanned: number;
  elapsed_ms: number;
  sort_by: string;
  /** 实际生效的条件树（可用于回显/复用） */
  conditions: Record<string, unknown>;
  results: ScreenRow[];
  provenance: Record<string, unknown>;
  degraded: boolean;
  degraded_reason?: string | null;
  fallback_tried?: unknown;
  provider_policy_version?: string;
  dataset_snapshot_id?: string | null;
  fundamentals?: unknown;
}

export interface ScreenRunQuery {
  /** 条件树 JSON 字符串（**必填**，后端 _parse_conditions 解析） */
  conditions: string;
  limit?: number;
  sort_by?: string;
  sort_desc?: number;
  adjust?: string;
  period?: string;
  min_price?: string;
  max_price?: string;
  max_codes?: number;
  /** auto / prefer_qmt / qmt_only / local_only / explicit:<id> */
  source_policy?: string;
  universe?: string;
  prefilter?: string;
  fields?: string;
  offline?: number;
}

/** 自然语言选股解析结果（**只解析不执行**，条件树与 /market/screen 兼容）。 */
export interface NlScreenResult {
  conditions?: Record<string, unknown>;
  rules?: unknown;
  unsupported?: unknown;
  [k: string]: unknown;
}

/**
 * 选股 API。路径与 backend/app/routes/screen.py 对应。
 *
 * ★ 契约修正（相对初版）：
 *   - GET /market/screen 的 conditions 是**必填** JSON 字符串
 *   - POST /market/screen/boards 是「把选股结果存为动态板块」，**不是**「板块内选股」；
 *     body 须含 name(str) / conditions(obj) / results(list)
 *   - GET /market/screen/boards 列出已保存的动态板块，返回 {items, count}
 *   - POST /market/screen/nl 只返回可编辑条件树，需再调 run/expr 才会真正执行
 */
export const screenApi = {
  run: (query: ScreenRunQuery) =>
    http.get<ScreenResponse>("/market/screen", { query: { ...query } }),

  /** 公式 DSL 选股（如 "C > MA(20) AND RSI(14) < 30"） */
  expr: (body: {
    expr: string;
    limit?: number;
    sort_by?: string;
    sort_desc?: number;
    adjust?: string;
    period?: string;
    source_policy?: string;
    universe?: unknown;
    prefilter?: unknown;
    fields?: unknown;
    offline?: number;
  }) => http.post<ScreenResponse>("/market/screen/expr", body),

  /** 把选股结果存为动态板块 */
  saveBoard: (body: {
    name: string;
    conditions: Record<string, unknown>;
    results: Array<{ code: string; name?: string; close?: number; change_pct?: number }>;
  }) => http.post<Record<string, unknown>>("/market/screen/boards", body),

  /** 已保存的动态板块列表 */
  boards: () =>
    http.get<{ items: Array<Record<string, unknown>>; count: number }>("/market/screen/boards"),

  /** 自然语言 → 可编辑条件树（不执行） */
  nl: (text: string) => http.post<NlScreenResult>("/market/screen/nl", { text }),
};
