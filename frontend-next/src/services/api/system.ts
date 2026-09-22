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

/**
 * 外观配置（`/config/ui`）。
 *
 * ⚠️ 后端**不认识**皮肤目录（那在前端 ``design/skins.ts``），只做**存储 + 形状校验**；
 * 所以后端返回的 ``fields`` 是它认识的字段名清单，不要指望它校验皮肤 id 是否真实存在。
 * 服务器这份是**权威**：界面本地仍有一份 localStorage（离线可用），但换机器 /
 * 清缓存后能从服务器拉回来 —— 此前外观只存 localStorage，换台机器就回到默认皮肤。
 */
export interface UiAppearance {
  skin_id: string;
  /** #RGB / #RRGGBB；空 = 用主题默认 */
  accent: string;
  /** compact / comfortable */
  density: string;
  wallpaper: string;
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
  trading_session: {
    mode: string;
    active: boolean | null;
    /** 今日是否交易日（节假日为 false）。mode=weekday-fallback 时不可靠，需据 mode 标注。 */
    trading_day: boolean | null;
  };
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

/**
 * MCP 工具清单与接入信息（capabilities.py:capabilities_mcp）。
 *
 * ★ 为什么界面要读它：MCP 工具此前只能靠 `tools/list` 协议调用才能看见，
 * 界面完全无从得知自己有多少工具可给 Agent 用 —— 能力存在但不可发现。
 * 本端点把它变成可浏览的清单。
 */
export interface McpCapabilities {
  /** 工具名全表（与 tests/contracts/mcp_tools.json 基线一致） */
  tools: string[];
  count: number;
  /** 按「首个下划线前的前缀」粗分组，仅用于界面浏览，不是权威分类 */
  by_prefix: Record<string, number>;
  endpoint: string;
  transport: string;
  auth: string;
  bind: string;
  /** 非空 = 当前用的是默认密钥远程监听风险提示 */
  local_only_note: string;
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

  /** MCP 工具清单（自省），用于界面浏览 + 接入说明 */
  capabilitiesMcp: () => http.get<McpCapabilities>("/capabilities/mcp"),

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

  /* ---- 外观（服务器权威，换机器/清缓存能拉回来） ---- */

  uiAppearance: () =>
    http.get<{ appearance: UiAppearance; defaults: UiAppearance; fields: string[] }>(
      "/config/ui",
    ),

  /** 可按字段部分提交（后端合并后再落库） */
  updateUiAppearance: (patch: Partial<UiAppearance>) =>
    http.put<{ saved: boolean; changed: string[]; appearance: UiAppearance }>(
      "/config/ui",
      patch,
    ),

  resetUiAppearance: () =>
    http.post<{ reset: boolean; appearance: UiAppearance }>("/config/ui/reset"),

  /** 导出：可抄给另一台机器（返回体本身即导入所需格式） */
  exportUiAppearance: () =>
    http.get<{ version: number; kind: string; exported_at: string; appearance: UiAppearance }>(
      "/config/ui/export",
    ),

  /** 导入：接受导出对象本身，也接受它的 appearance 字段 */
  importUiAppearance: (body: Record<string, unknown>) =>
    http.post<{ imported: boolean; changed: string[]; appearance: UiAppearance }>(
      "/config/ui/import",
      body,
    ),

  /* ---- 通知渠道 ---- */

  notifications: () => http.get<NotificationConfig[]>("/notifications"),

  /** 带 id 即为更新（后端按 id 走 UPDATE） */
  saveNotification: (body: NotificationPayload) =>
    http.post<{ id: number }>("/notifications", body),

  deleteNotification: (nid: number) => http.del<{ deleted: boolean }>(`/notifications/${nid}`),

  /** ★ 批量删除的 body 键是 ids（与 alerts / webhooks / api-keys 一致） */
  batchDeleteNotifications: (ids: number[]) =>
    http.post<{ deleted: number }>("/notifications/batch-delete", { ids }),

  dataProviders: () => http.get<DataProvidersResponse>("/data/providers"),
  dataProvidersHealth: () => http.get<DataProvidersHealth>("/data/providers/health"),
  datahubPolicies: () => http.get<DatahubPolicies>("/datahub/policies"),
  sourceDiagnostics: (params: { capability?: string; probe?: number } = {}) =>
    http.get<SourceDiagnostics>("/data/source/diagnostics", { query: params }),



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

/** 选股结果行（evaluate_scan / run_classic 产出的行结构）。 */
export interface ScreenRow {
  code: string;
  name?: string;
  close?: number;
  change_pct?: number;
  score?: number;
  /** 经典策略：命中的策略 id（run_classic 补） */
  strategy?: string;
  [k: string]: unknown;
}

/** 经典策略元信息（GET /market/screen/strategies）。 */
export interface ClassicStrategy {
  id: string;
  label: string;
  desc: string;
  params: Record<string, number>;
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
  /** 走经典策略时回显策略 id；空串表示走的是条件树 */
  classic?: string;
}

export interface ScreenRunQuery {
  /**
   * 条件树 JSON 字符串。
   *
   * ★ 2026-09-18 起**不再无条件必填**：传 `classic` 走经典策略时可省略
   * （后端此时只回显 conditions、不参与求值）。两者都不传 → 400 并给出原因。
   */
  conditions?: string;
  /** 经典策略 id（见 screenApi.strategies）；非空时 conditions 不参与求值 */
  classic?: string;
  /** 覆盖策略默认参数的 JSON 字符串，如 '{"breakout_days":30}' */
  classic_params?: string;
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
 * 一次选股运行的可信度元数据（`screen_runs` 表，GET /market/screen/classic/picks）。
 *
 * ★ 这几个字段不是装饰：`hits === 0` 时它们是**唯一**能区分
 * 「行情不好所以没选中」与「根本没扫到数据（同步没跑成）」的依据。
 */
export interface ScreenRunMeta {
  run_id: string;
  /** manual（界面手动跑）/ schedule（定时任务） */
  source: string;
  job_id: string;
  strategies: string[];
  hits: number;
  /** 本次扫描了多少只；**0 表示根本没拿到数据**，不是「没选中」 */
  scanned: number;
  /** 命中所依据的日线截至日 YYYYMMDD（★ 非空 ≠ 够新）；解析不了为空串 */
  bar_date: string;
  degraded: boolean;
  degraded_reason: string;
  /** 命中过多被截断（只落了前 N 条） */
  truncated: boolean;
  created_at: string;
}

/** 一条命中（含「为什么选中它」的 reason 与其余策略明细）。 */
export interface ScreenPick {
  strategy: string;
  code: string;
  name: string;
  close: number | null;
  change_pct: number | null;
  score: number | null;
  reason: string;
  detail: Record<string, unknown>;
}

/** GET /market/screen/classic/picks 的返回体。 */
export interface ClassicPicksResponse {
  /** null = 库里还没有任何选股记录（界面须显示「尚未跑过」，不是空表格） */
  run: ScreenRunMeta | null;
  picks: ScreenPick[];
  available_runs: ScreenRunMeta[];
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
 *   - GET /market/screen/strategies 列举经典策略（id/label/desc/params）
 *   - POST /market/screen/classic 一次跑多个经典策略，按策略 id 分组返回
 *   - GET /market/screen 传 `classic` 时走经典策略分支，此时 conditions 可省略
 */
export const screenApi = {
  run: (query: ScreenRunQuery) =>
    http.get<ScreenResponse>("/market/screen", { query: { ...query } }),

  /** 经典策略清单（用于下拉与参数表单） */
  strategies: () =>
    http.get<{ items: ClassicStrategy[]; count: number }>("/market/screen/strategies"),

  /**
   * 最近一次（或指定 `run_id`）的选股结果 —— 「自动选股」页签的数据源。
   *
   * ★ 为什么需要它：定时选股跑完之后，用户此前只能去「任务运行时」翻一个巨大的
   *   JSON 结果字段，翻不到就等于没有。本接口让结果**有稳定的界面**。
   *   `run` 为 null 表示**从未跑过**（显示「尚未跑过选股」，不是空表格 ——
   *   空表格会被读成「今天没选出票」）。
   */
  classicPicks: (opts: { runId?: string; strategy?: string; source?: string; limit?: number } = {}) =>
    http.get<ClassicPicksResponse>("/market/screen/classic/picks", {
      query: {
        run_id: opts.runId ?? "",
        strategy: opts.strategy ?? "",
        source: opts.source ?? "",
        limit: opts.limit ?? 500,
      },
    }),

  /** 经典策略选股（多策略批量，与定时任务 system.classic_screen 同一内核） */
  classic: (body: {
    strategies: string[];
    params?: Record<string, Record<string, number>>;
    limit?: number;
    universe?: unknown;
    source_policy?: string;
    adjust?: string;
    period?: string;
    max_codes?: number;
    offline?: number;
  }) =>
    http.post<{
      strategies: string[];
      scanned: number;
      total_hits: number;
      results: Record<string, ScreenRow[]>;
      degraded: boolean;
      degraded_reason?: string;
      provenance: Record<string, unknown>;
      /** 本次落库的运行摘要（run_id 可用于「自动选股」页签定位） */
      run?: { run_id: string; saved: number; total_hits: number; error?: string };
    }>("/market/screen/classic", body),

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

/* ---------------- 数据面（datahub.py：数据源矩阵 / 健康 / 限流策略） ---------------- */

/** 单个数据源画像（`/data/providers.providers[]`）。 */
export interface DataProviderInfo {
  provider: string;
  name?: string;
  transport?: string;
  capabilities?: string[];
  active?: boolean;
  dependency_available?: boolean;
  commercial_ok?: boolean;
  status?: string;
  requires?: string;
  optional_dependency?: string;
  license_note?: string;
  [k: string]: unknown;
}

/** 数据源矩阵（`/data/providers`）—— 除 providers 外还有降级链与选股可用性。 */
export interface DataProvidersResponse {
  chain_version?: string;
  default_source_policy?: string;
  commercial_mode?: boolean;
  capability_chains?: Record<string, string[]>;
  chain_resolved?: Record<string, string[]>;
  providers?: DataProviderInfo[];
  screening_ready?: boolean;
  screening_providers?: string[];
  local_data_available?: boolean;
  [k: string]: unknown;
}

/** 逐源健康探测（`/data/providers/health`）。 */
export interface DataProvidersHealth {
  providers?: DataProviderInfo[];
  [k: string]: unknown;
}

/** 限流策略项（`/datahub/policies`）：ttl / 最小间隔 / 合并窗口 / 优先级。 */
export interface DatahubPolicy {
  ttl_ms?: number;
  min_interval_ms?: number;
  coalesce_within_ms?: number;
  priority?: string;
  stale_ok?: boolean;
  [k: string]: unknown;
}

export interface DatahubPolicies {
  default?: DatahubPolicy;
  topics?: Record<string, DatahubPolicy>;
  [k: string]: unknown;
}

/** 数据源失败溯源（`/data/source/diagnostics`）。
 *  `chain` —— 本应被尝试的源；空列表 ≠ 网络坏，而是「该源刻意不提供该能力」。
 *  `tried` —— 实际尝试的过程与每步原因。 */
export interface SourceDiagnostics {
  capability?: string;
  probed?: boolean;
  chain?: string[];
  tried?: string[];
  interpretation?: string;
  [k: string]: unknown;
}
