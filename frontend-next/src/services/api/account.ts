import { http } from "../http";
import type {
  AccountGrid,
  AccountStatus,
  BatchItemResult,
  BatchOrderSummary,
  PriceType,
  Side,
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
 *
 * ★ 零消费方法登记（2026-10-13 R2 审计）：
 *   `status` / `batchOrder` / `batchCancel` 目前无前端调用点，但**刻意保留**：
 *   三者都是后端已实现且契约稳定的账户域能力（/account/status 是模拟盘账本的
 *   入口，/account/batch/* 是多账户批处理的入口）。当前页面走的是各自的领域页
 *   （Paper / Accounts），并未直接引这里的封装，但「批量下单 / 批量撤单」是
 *   账户域的核心能力，一旦 UI 需要就会用到。删除会误导后续开发者以为这些
 *   端点不存在；因此显式声明「零调用但合法」，而非默默留着。
 *   若后续 6 个月内仍无人调用，再评估删除（参照 services/http.ts 里
 *   「零调用但合法」的先例处理方式：先声明，不急于删）。
 */
export const accountApi = {
  status: (connId = "") =>
    http.get<AccountStatus>("/account/status", { query: { conn_id: connId } }),

  grid: () => http.get<AccountGrid>("/account/grid"),

  /** 批量下单：传 orders 逐单形态（推荐，可逐账户差异化） */
  batchOrder: (orders: BatchOrderItem[]) =>
    http.post<BatchOrderSummary>("/account/batch/order", { orders }),

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
