import { http } from "../http";
import type {
  AccountAggregate,
  AccountGrid,
  AccountStatus,
  BatchItemResult,
  BatchOrderSummary,
  NetValuePoint,
  PriceType,
  Side,
  SlippageReport,
} from "@/shared/types";

/** 批量下单的单条指令（展开形态，account.py:_expand_batch_orders）。 */
export interface BatchOrderItem {
  conn_id: string;
  code: string;
  direction: Side;
  volume: number;
  price: number;
  price_type?: PriceType;
}

/** 广播形态：同一指令发往多个账户。 */
export interface BatchBroadcast {
  conn_ids: string[];
  code: string;
  direction: Side;
  volume: number;
  price: number;
  price_type?: PriceType;
}

/**
 * 账户域 API。路径与 backend/app/routes/account.py 对应。
 *
 * ★ 契约修正（相对初版）：
 *   - status 返回 assets（非 total_assets），并带 positions 数组
 *   - batch/order 的 body 是 {orders:[...]} 或广播字段，**不是** {conn_ids, items}
 *   - batch/cancel 的 body 是 {items:[{conn_id,order_id}]}，**不是** {conn_ids, order_ids}
 */
export const accountApi = {
  status: (connId = "") =>
    http.get<AccountStatus>("/account/status", { query: { conn_id: connId } }),

  aggregate: () => http.get<AccountAggregate>("/account/aggregate"),

  /**
   * 净值曲线。
   *
   * ★ 多账户必须传 `accountId`：不传时后端返回的是**所有账户混在一起的曲线**
   * （`mixed_accounts=true`），形状完全失真。消费方拿到 `mixed_accounts` 应显式
   * 提示用户，不要默默画出来。
   */
  pnl: (accountId = "") =>
    http.get<{
      net_value_series: NetValuePoint[];
      account_id: string;
      accounts: string[];
      mixed_accounts: boolean;
    }>("/account/pnl", { query: { account_id: accountId } }),

  /** 滑点分析。★ code 必填：后端已移除写死的默认标的，不传会 400 */
  slippage: (code: string, connId = "") =>
    http.get<SlippageReport>("/account/slippage", { query: { code, conn_id: connId } }),

  grid: () => http.get<AccountGrid>("/account/grid"),

  /** 批量下单：传 orders 逐单形态（推荐，可逐账户差异化） */
  batchOrder: (orders: BatchOrderItem[]) =>
    http.post<BatchOrderSummary>("/account/batch/order", { orders }),

  /** 批量下单：广播形态（同一指令发往多账户） */
  batchOrderBroadcast: (body: BatchBroadcast) =>
    http.post<BatchOrderSummary>("/account/batch/order", body),

  /** 批量撤单：逐单指定 conn_id + order_id */
  batchCancel: (items: Array<{ conn_id: string; order_id: string }>) =>
    http.post<{ total: number; ok: number; results: BatchItemResult[] }>(
      "/account/batch/cancel",
      { items },
    ),

  batchReconnect: (connIds: string[]) =>
    http.post<{ total: number; results: BatchItemResult[] }>("/account/batch/reconnect", {
      conn_ids: connIds,
    }),
};

export type { BatchItemResult };
