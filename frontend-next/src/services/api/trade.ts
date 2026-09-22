import { http } from "../http";
import type {
  ConditionOrder,
  ConditionStatusPayload,
  Deal,
  Order,
  Position,
  PriceType,
  Side,
} from "@/shared/types";

/**
 * 预检结果（trade.py:trade_precheck 的真实返回）。
 * ★ 字段是 allowed，不是 ok；也没有 est_amount / requires_totp。
 * 若需预估金额，请由前端按「价格 × 数量」自行计算并明示为「估算」。
 */
export interface PrecheckResult {
  allowed: boolean;
  reason: string;
}

export interface SubmitOrderPayload {
  conn_id?: string;
  code: string;
  direction: Side;
  volume: number;
  price: number;
  price_type?: PriceType;
  remark?: string;
  idempotency_key?: string;
}

/**
 * 下单结果。三种形态必须区分清楚，前端不得混淆：
 * - 已提交成功：ok=true 且 order_id 存在
 * - 挂起待确认：pending_confirmation=true，需再调 confirmSignal
 * - 被拒/失败：ok=false，reason 为真实原因（风控拦截 / 柜台拒单 / 未连券商）
 */
export interface OrderResult {
  ok: boolean;
  order_id?: string;
  status?: string;
  reason?: string;
  mode?: string;
  conn_id?: string;
  pending_confirmation?: boolean;
  confirm_token?: string;
  amount?: number;
  requires_totp?: boolean;
  broker_unavailable?: boolean;
}

/**
 * 目标持仓调仓载荷（trade.py:trade_target → tools.position.order_target_position）。
 * ★ 后端字段名是 target_pct（比例 0–1），price=0 表示市价，
 *   do_trade=false 仅测算不动账，true 才真实下单。
 */
export interface TargetPositionPayload {
  code: string;
  target_pct: number;
  price?: number;
  do_trade?: boolean;
}

export interface ConditionOrderPayload {
  code: string;
  side: Side;
  /** gte = 价格≥触发价（突破买入/止损卖出）；lte = 价格≤触发价 */
  trigger_type: "gte" | "lte";
  /** ★ 后端字段名为 trigger_price，非 trigger_value */
  trigger_price: number;
  /** 须为 100 的整数倍 */
  volume: number;
  price_type?: PriceType;
  price?: number;
  remark?: string;
  /** 0 = 仅当日有效；N = N 个自然日内有效 */
  valid_days?: number;
}

/** 条件单提交返回（engines/condition_order.py:submit）。 */
export interface ConditionCreated {
  id: string;
  status: string;
  expire_at: string;
}

/**
 * 交易域 API。路径与 backend/app/routes/trade.py 一一对应。
 *
 * ⚠ 契约要点：positions / orders / deals **只作用于 active 连接**，
 *   且 positions 的 query 参数是 symbol（按标的过滤），不是 conn_id。
 *   多账户视图请用 accountApi.grid / accountApi.aggregate。
 */
export const tradeApi = {
  precheck: (body: SubmitOrderPayload) => http.post<PrecheckResult>("/trade/precheck", body),

  order: (body: SubmitOrderPayload) => http.post<OrderResult>("/trade/order", body),

  cancel: (orderId: string, connId = "") =>
    http.post<{ ok: boolean; reason?: string }>("/trade/cancel", {
      order_id: orderId,
      conn_id: connId,
    }),

  /** symbol 为空则返回全部持仓 */
  positions: (symbol = "") =>
    http.get<Position[]>("/trade/positions", { query: { symbol } }),

  orders: () => http.get<Order[]>("/trade/orders"),

  deals: () => http.get<Deal[]>("/trade/deals"),

  /** 条件单引擎整体状态（含 running / total / pending / orders） */
  conditions: () => http.get<ConditionStatusPayload>("/trade/conditions"),

  createCondition: (body: ConditionOrderPayload) =>
    http.post<ConditionCreated>("/trade/conditions", body),

  cancelCondition: (cid: string) =>
    http.post<{ ok: boolean }>(`/trade/conditions/${cid}/cancel`),

  /**
   * 目标持仓调仓（POST /trade/target）。
   * do_trade=false 时后端仅测算差额不动账，前端须如实展示「测算」而非「已调仓」。
   */
  target: (body: TargetPositionPayload) =>
    http.post<Record<string, unknown>>("/trade/target", body),
};

export type { ConditionOrder };

/** 信号模式（gateway/signal_router.py:set_mode 只接受这三种）。 */
export type SignalMode = "live" | "paper" | "dry_run";

/** 信号提交载荷。★ 字段是 side（不是 direction）。 */
export interface SignalSubmitPayload {
  source?: string;
  code: string;
  side: Side;
  volume: number;
  price?: number;
  price_type?: PriceType;
  remark?: string;
  broker_id?: string;
  idempotency_key?: string;
  payload?: Record<string, unknown>;
}

/** 信号路由 API（含二次确认流）。路径与 backend/app/routes/signal.py 对应。 */
export const signalApi = {
  getMode: () => http.get<{ mode: SignalMode }>("/signal/mode"),

  /** ★ 仅支持 live / paper / dry_run（无 paused） */
  setMode: (mode: SignalMode) => http.post<{ mode: SignalMode }>("/signal/mode", { mode }),

  /** ★ body 用 side；成功走 ok()，失败统一 503 并带 reason */
  submit: (body: SignalSubmitPayload) => http.post<OrderResult>("/signal/submit", body),

  /**
   * 大额单二次确认。
   *
   * ★ 字段名是 `totp_code`，**不是** `totp`（2026-09-20 修复）。
   * 此前这里发 `totp`、后端 `routes/signal.py` 只读 `totp_code`，两侧各用一个
   * 名字且中间无归一化 ⇒ **一旦启用 TOTP，大额单二次确认永远失败**，还报
   * 「TOTP 校验失败，请重新发起信号」把责任推给用户（重试多少次都没用）。
   * 后端现已同时接受两种写法（兼容已分发的客户端），此处统一发规范名。
   *
   * ★ 不需要回传 price_type：挂起时整份 signal（含 price_type）已存在后端
   * `_pending` 里，`confirm()` 用 `Signal(**entry["sig"])` 原样恢复。
   * （此处旧注释声称「必须回传 price_type」是过时的，且 confirm 路由根本不读它。）
   */
  confirm: (token: string, totp?: string) =>
    http.post<OrderResult>("/signal/confirm", { confirm_token: token, totp_code: totp ?? "" }),
};
