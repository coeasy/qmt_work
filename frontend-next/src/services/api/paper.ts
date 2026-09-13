import { http } from "../http";
import type { PriceType, Side } from "@/shared/types";

/**
 * 模拟盘（Paper Trading）API 层。
 *
 * ★ 口径来源：backend/app/routes/paper.py + engines/paper_engine.py（逐字段核对）。
 *   - POST /paper/reset      {initial_capital?} → 返回**重置后的 account**（不是 ok 标志）
 *   - POST /paper/order      {code, side, price, volume, price_type?, remark?}
 *   - GET  /paper/account    现金/市值/总资产/浮动与已实现盈亏（含 positions 数组）
 *   - GET  /paper/positions  持仓列表
 *   - GET  /paper/trades?limit=50  → 元素为 {id, code, side, price, volume, ts, pnl}
 *   - GET  /paper/metrics    绩效摘要（胜率/均值/最好最差）
 *
 * 零 mock：模拟盘引擎未初始化时后端返回 503，http.ts 原样抛 ApiError，本层不粉饰。
 * 页面据此显式提示「引擎未就绪」，而不是展示一张空表冒充「没有成交」。
 */

/** 单条持仓（paper_engine.get_positions）。 */
export interface PaperPosition {
  code: string;
  name: string;
  volume: number;
  avg_cost: number;
  last_price: number;
  side?: string;
  market_value: number;
  cost: number;
  unrealized_pnl?: number;
  /** 盯市来源：live=真实行情价；cost=退化成本价（无行情） */
  marking?: "live" | "cost";
}

/** 账户概览（paper_engine.get_account，含内嵌 positions）。 */
export interface PaperAccount {
  cash: number;
  market_value: number;
  total_assets: number;
  initial: number;
  unrealized_pnl: number;
  realized_pnl: number;
  /** 小数形态（0.0123 = 1.23%），展示需 ×100 */
  total_return: number;
  position_count: number;
  /** live=任一持仓有真实行情；frozen=全部退化为成本价 */
  marking_source: "live" | "frozen";
  positions: PaperPosition[];
}

/** 单条成交（paper_trades 表原始列，注意主键是 id 不是 trade_id）。 */
export interface PaperTrade {
  id: number;
  code: string;
  side: Side;
  price: number;
  volume: number;
  ts: string;
  /** 仅卖出平仓有值；买入建仓为 0/null */
  pnl?: number | null;
}

/** 绩效摘要（paper_engine.metrics）。 */
export interface PaperMetrics {
  trade_count: number;
  close_count: number;
  win_count: number;
  win_rate: number;
  avg_pnl: number;
  best_pnl: number;
  worst_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
}

export interface PaperOrderInput {
  code: string;
  side: Side;
  price: number;
  volume: number;
  price_type?: PriceType;
  remark?: string;
}

export const paperApi = {
  account: () => http.get<PaperAccount>("/paper/account"),

  positions: () => http.get<PaperPosition[]>("/paper/positions"),

  trades: (limit = 50) =>
    http.get<PaperTrade[]>("/paper/trades", { query: { limit } }),

  metrics: () => http.get<PaperMetrics>("/paper/metrics"),

  /** 模拟下单：立即以给定价格全额成交；现金/可卖不足时后端抛 400。 */
  order: (body: PaperOrderInput) => http.post<Record<string, unknown>>("/paper/order", body),

  /** 重置：清空持仓与成交、现金回到 initial_capital；返回重置后的账户。 */
  reset: (initialCapital?: number) =>
    http.post<PaperAccount>("/paper/reset", { initial_capital: initialCapital ?? 1_000_000 }),
};
