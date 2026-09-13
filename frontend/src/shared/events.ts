// V10 Phase D (D5/D6)：WS 事件词汇表单一真源（TS 化）。
// 后端真源：backend/tests/contracts/ws_events.json（AST 扫描 emit 调用点生成）。
// 订阅端只允许引用本表常量；新增事件须先在后端 emit + 刷新契约快照。
// 原 lib/events.js 的 UI 侧 EVENT_TYPES/normalizePlatformEvent 已并入此处（D6 去重）。

/** 后端 WS 推送的事件类型（与 ws_events.json 契约对齐）。 */
export const WS_EVENTS = {
  QUOTES: "quotes",               // 行情微批帧
  ORDER: "order",                 // 委托回报
  DEAL: "deal",                   // 成交回报（ws_events.json 契约原生 type）
  TRADE: "trade",                 // 成交回报（历史别名，部分组件沿用）
  ALGO: "algo",                   // 算法单进度/状态
  CONDITION: "condition",         // 条件单触发/状态
  LIMITUP: "limitup",             // 涨停触发
  ACCOUNT: "account",             // 账户快照/健康
  SYSTEM: "system",               // 连接健康/调度/运维事件
  RECONNECTED: "reconnected",     // 断线重连完成（含 30s 事件窗口补发标记）
  PING: "ping",
  PONG: "pong",
} as const;

export type WsEventType = (typeof WS_EVENTS)[keyof typeof WS_EVENTS];

export const WS_CHANNELS: string[] = ["system", "account", "order", "deal", "quote"];

/** 判定一帧是否为合法事件信封：{type: <WS_EVENTS>, data, ts} */
export function isKnownEvent(type: unknown): boolean {
  return Object.values(WS_EVENTS).includes(type as WsEventType);
}

// ---------------- UI 侧事件归一化（原 lib/events.js，D6 并入） ----------------

/**
 * UI 抽象事件类型：WS 契约词汇的子集 + 平台内部事件别名
 * （quote/connection/job/dataset 为 UI 聚合层词汇，非后端 WS 原生 type）。
 */
export const EVENT_TYPES = Object.freeze({
  QUOTE: "quote",
  ORDER: "order",
  TRADE: "trade",
  ACCOUNT: "account",
  CONNECTION: "connection",
  JOB: "job",
  DATASET: "dataset",
});

export type PlatformEventType = (typeof EVENT_TYPES)[keyof typeof EVENT_TYPES];

export interface PlatformEvent {
  type: string;
  ts: string;
  connectionId: string;
  data: Record<string, unknown>;
}

/** 把任意来源的事件帧归一化为 UI 可消费形态；未知类型返回 null（拒绝脏数据）。 */
export function normalizePlatformEvent(event: unknown): PlatformEvent | null {
  if (!event || typeof event !== "object") return null;
  const raw = event as Record<string, unknown>;
  const type = String(raw.type || "");
  if (!Object.values(EVENT_TYPES).includes(type as PlatformEventType)) return null;
  const ts = raw.ts ?? raw.timestamp;
  const data = raw.data;
  return {
    type,
    ts: typeof ts === "string" && ts ? ts : new Date().toISOString(),
    connectionId: String(raw.connection_id || raw.conn_id || ""),
    data: (data && typeof data === "object" ? data : {}) as Record<string, unknown>,
  };
}
