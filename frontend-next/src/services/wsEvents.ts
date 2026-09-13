import type { WsMessage } from "@/shared/types";

/**
 * WS 事件契约与前端消费策略（与 backend/tests/contracts/ws_events.json 对齐）。
 *
 * 后端事件有两种形态：
 * 1. 频道 + 子类型：deal/order/risk/quotes；
 * 2. 直接事件类型：signal_live、risk.blocked、order.timeout 等。
 *
 * SystemLog 是全量兜底消费者：所有事件都会进入实时日志流；`handler` 标出是否还有
 * 领域级消费者。这样「系统日志能看到」与「行情/成交页面真正处理」不会被混为一谈。
 * 新增后端事件时，先刷新 ws_events.json，再在此登记消费策略，契约门禁会检查两边。
 */
export type WsEventHandler = "quotes-store" | "deal-feed" | "system-log";

export const WS_EVENT_REGISTRY: Record<string, WsEventHandler> = {
  // 频道事件：服务端 type 是频道，payload 内可能再带 data.type
  "quotes": "quotes-store",
  "snapshot": "quotes-store",
  "quotes_replay": "system-log",
  "heartbeat": "system-log",
  "pong": "system-log",
  "deal": "deal-feed",
  "deal_event": "deal-feed",
  "order": "deal-feed",
  "order_event": "deal-feed",
  "risk": "system-log",
  "risk_circuit": "system-log",
  "system": "system-log",

  // 引擎事件：当前由 SystemLog 全量记录；领域页面可在后续按需升级专用消费者
  "algo_alert": "system-log",
  "algo_slice": "system-log",
  "condition_created": "system-log",
  "condition_expired": "system-log",
  "condition_failed": "system-log",
  "condition_order": "system-log",
  "condition_settled": "system-log",
  "condition_triggered": "system-log",
  "limitup": "system-log",
  "limitup_order": "system-log",
  "order.timeout": "system-log",
  "reconcile": "system-log",
  "risk.blocked": "system-log",
  "signal_dry_run": "system-log",
  "signal_live": "system-log",
  "signal_paper": "system-log",
  "signal_pending": "system-log",
};

export type KnownWsEvent = keyof typeof WS_EVENT_REGISTRY;

export function isKnownWsEvent(type: unknown): type is KnownWsEvent {
  return typeof type === "string" && type in WS_EVENT_REGISTRY;
}

/** 事件日志的统一短摘要，避免各页面重复解析 WS envelope。 */
export function wsEventLabel(msg: WsMessage): string {
  const type = typeof msg.type === "string" ? msg.type : "system";
  return isKnownWsEvent(type) ? type : `unknown:${type}`;
}
