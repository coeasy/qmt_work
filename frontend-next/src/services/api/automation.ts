import { http } from "../http";
import type {
  AlertHistoryRow,
  AlertRule,
  AlgoKind,
  AlgoOrder,
  LimitUpStatus,
  PriceType,
  Side,
  WalStats,
  WebhookDelivery,
  WebhookSub,
} from "@/shared/types";

/**
 * 算法单提交载荷（algo.py:algo_submit）。
 * ★ 后端读的是 direction / algo，不是 side / algo_type。
 */
export interface AlgoSubmitPayload {
  code: string;
  direction: Side;
  /** 须为 100 的整数倍 */
  volume: number;
  algo: AlgoKind;
  /** 总时长（秒），后端下限 10 */
  duration?: number;
  /** 切片数，后端夹取到 1–50 */
  slices?: number;
  price_type?: PriceType;
  limit_price?: number;
  remark?: string;
  /** 冰山单可见比例 %（1–100） */
  visible_pct?: number;
  /** POV 参与率（0.01–1.0） */
  participation_rate?: number;
}

/** 算法交易 API。路径与 backend/app/routes/algo.py 对应。 */
export const algoApi = {
  submit: (body: AlgoSubmitPayload) => http.post<AlgoOrder>("/algo/submit", body),
  list: () => http.get<AlgoOrder[]>("/algo"),
  pause: (id: string) => http.post<AlgoOrder>(`/algo/${id}/pause`),
  resume: (id: string) => http.post<AlgoOrder>(`/algo/${id}/resume`),
  cancel: (id: string) => http.post<AlgoOrder>(`/algo/${id}/cancel`),
};

export interface LimitUpStartOptions {
  limit_pct?: number;
  cutoff?: string;
  min_rise?: number;
  buy_volume?: number;
  do_trade?: boolean;
  interval?: number;
}

/**
 * 涨停监控 API。路径与 backend/app/routes/limitup.py 对应。
 * ★ 股票池是**单只**增删（后端 add(code) / remove(code)），不是批量数组。
 */
export const limitupApi = {
  status: () => http.get<LimitUpStatus>("/limitup/status"),
  addPool: (code: string) => http.post<{ code: string; name: string }>("/limitup/pool", { code }),
  removePool: (code: string) =>
    http.del<{ removed: string }>("/limitup/pool", { query: { code } }),
  start: (opts: LimitUpStartOptions = {}) =>
    http.post<Record<string, unknown>>("/limitup/start", opts),
  stop: () => http.post<Record<string, unknown>>("/limitup/stop"),
  reset: () => http.post<{ reset: boolean }>("/limitup/reset"),
};

/** 告警规则载荷（alerts.py:save_alert_rule）。id 存在即为更新。 */
export interface AlertRulePayload {
  id?: number;
  name: string;
  enabled?: boolean;
  /** 事件类型，默认 "*" */
  event?: string;
  metric?: string;
  op?: string;
  threshold?: number;
  /** 通知渠道，默认 "*" */
  channel?: string;
  cooldown_seconds?: number;
}

/**
 * 告警规则 API。路径与 backend/app/routes/alerts.py 对应。
 * ★ 主键字段是 id（int），batch-delete 的 body 键是 ids。
 */
export const alertApi = {
  rules: () => http.get<AlertRule[]>("/alerts/rules"),
  saveRule: (body: AlertRulePayload) => http.post<{ id: number }>("/alerts/rules", body),
  deleteRule: (rid: number) => http.del<{ deleted: boolean }>(`/alerts/rules/${rid}`),
  batchDelete: (ids: number[]) =>
    http.post<{ deleted: number }>("/alerts/rules/batch-delete", { ids }),
  test: (event: string, payload: Record<string, unknown> = {}) =>
    http.post<{ fired: boolean; event: string }>("/alerts/test", { event, payload }),
  history: (limit = 50) => http.get<AlertHistoryRow[]>("/alerts/history", { query: { limit } }),
};

/**
 * 出站 Webhook API。路径与 backend/app/routes/webhooks.py 对应。
 * ★ 主键字段是 id（int）；batch-delete 的 body 键是 ids。
 */
export const webhookApi = {
  list: () => http.get<WebhookSub[]>("/webhooks"),
  create: (body: { name?: string; url: string; events?: string; enabled?: boolean }) =>
    http.post<{ id: number }>("/webhooks", body),
  remove: (sid: number) => http.del<{ deleted: boolean }>(`/webhooks/${sid}`),
  batchDelete: (ids: number[]) =>
    http.post<{ deleted: number }>("/webhooks/batch-delete", { ids }),
  test: (sid: number) => http.post<Record<string, unknown>>(`/webhooks/${sid}/test`),
  deliveries: (sid = 0, limit = 50) =>
    http.get<WebhookDelivery[]>("/webhooks/deliveries", { query: { sid, limit } }),
};

/** 目标持仓与再平衡 API。 */
export const portfolioApi = {
  /** targets 是 {code: 权重} 字典（后端 engine.sync 按 .items() 遍历），不是数组 */
  syncTarget: (body: { mode: "shares" | "amount" | "ratio"; targets: Record<string, number>; dry_run?: boolean }) =>
    http.post<Record<string, unknown>>("/target-portfolio/sync", body),
  plans: () => http.get<unknown[]>("/target-portfolio/plans"),
  createPlan: (body: Record<string, unknown>) =>
    http.post<{ pid: string }>("/target-portfolio/plans", body),
  deletePlan: (pid: string) => http.del<{ ok: boolean }>(`/target-portfolio/plans/${pid}`),
  /** ★ 批量删除的 body 键是 ids（与 alerts / webhooks / api-keys 一致） */
  batchRemovePlans: (ids: number[]) =>
    http.post<{ deleted: number }>("/target-portfolio/plans/batch-delete", { ids }),
  rebalance: (body: Record<string, unknown>) =>
    http.post<Record<string, unknown>>("/rebalance", body),
};

/** 对账核销 API。路径与 backend/app/routes/reconcile.py 对应。 */
export const reconcileApi = {
  /** 立即执行委托对账核销（body 可选 {conn_id}） */
  run: (connId = "") => http.post<Record<string, unknown>>("/reconcile", { conn_id: connId }),

  /** 最近一次对账结果（无记录时后端返回 {checked: 0}） */
  last: () => http.get<Record<string, unknown>>("/reconcile/last"),

  walStats: () => http.get<WalStats>("/reconcile/wal/stats"),

  /** 手动触发 WAL 归档轮转 */
  walCheckpoint: () => http.post<{ checkpointed: boolean }>("/reconcile/wal/checkpoint"),
};
