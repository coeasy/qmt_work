import { http } from "../http";
import type {
  Bar,
  Instrument,
  MoneyFlowPoint,
  Period,
  Quote,
} from "@/shared/types";

/* ---------------- 聚合返回类型（口径来源：app/services/market/aggregates.py） ---------------- */

export interface KlineResponse {
  code: string;
  period: string;
  count: number;
  source?: string;
  cached_at?: string | null;
  /** 仅当 stale=true 时给出，表示数据截至时间 */
  as_of?: string | null;
  note?: string | null;
  adjust: string;
  /** true = 过期缓存回退，UI 必须显式标注，不得冒充最新 */
  stale: boolean;
  bars: Bar[];
}

export interface QuotesResponse {
  items: Quote[];
  served: number;
  requested: number;
}

export interface MinutePoint {
  t: string;
  price: number;
  avg?: number;
  volume?: number;
}

export interface MinutesResponse {
  code: string;
  trading_date: string;
  pre_close: number;
  open_price: number;
  points: MinutePoint[];
}

export interface StockInfo {
  code: string;
  name: string;
  exchange?: string;
  board?: string;
  high_limit?: number | null;
  low_limit?: number | null;
  pre_close?: number | null;
  industry?: string;
  concepts?: string[];
  source?: string;
  note?: string;
}

export interface IndicesResponse {
  items: Array<(Quote & { spark?: number[] | null }) | null>;
  codes: string[];
  errors: Record<string, string>;
  ts: string;
  source: string;
}

export interface BoardItem {
  code: string;
  name: string;
  change_pct?: number;
  amount?: number;
  last?: number;
  [k: string]: unknown;
}

export interface BoardsResponse {
  items: BoardItem[];
  kind: string;
  count: number;
  source: string;
  ts: string;
}

export interface BoardConstituentsResponse {
  items: Instrument[];
  has_more?: boolean;
  page?: number;
  total?: number;
  source?: string;
}

export interface BoardLookupResponse {
  name: string;
  matches: BoardItem[];
  source: string;
}

export interface BoardKlineResponse {
  code: string;
  period: string;
  bars: Bar[];
  source: string;
}

export interface EtfResponse {
  items: Array<Instrument & { last?: number; change_pct?: number; amount?: number }>;
  count: number;
  groups: Record<string, number>;
  quote_capped?: boolean;
  quote_limit?: number;
  source: string;
  ts: string;
}

export interface MoneyflowResponse {
  code: string;
  /** 内盘累计（手） */
  inside?: number | null;
  /** 外盘累计（手） */
  outside?: number | null;
  /** 外盘 - 内盘（手） */
  net?: number | null;
  /** 分钟级主买/主卖力道 */
  strength?: MoneyFlowPoint[];
  /** 量比 */
  volume_ratio?: number | null;
  /** false = 真实口径 */
  est?: boolean;
  ts?: string;
  source?: string;
}

export interface BoardMoneyflowResponse {
  code: string;
  count: number;
  total_net: number;
  total_inside: number;
  total_outside: number;
  contributors: Array<{ code: string; name: string; net: number; pct: number }>;
  /** 固定为 day：eltdx 仅提供日累计快照，不伪造分钟序列 */
  granularity: "day";
  source: string;
  ts: string;
}

export interface RotationBoard {
  code: string;
  name: string;
  total_pct?: number;
  daily: Array<{ date: string; pct: number }>;
  cum_pct: number;
}

export interface RotationResponse {
  days: number;
  kind: string;
  boards: RotationBoard[];
  source: string;
  ts: string;
}

export interface OverviewResponse {
  breadth: Array<{ code: string; name: string; count?: number; metric?: string; unit?: string }>;
  breadth_summary: {
    /** 上涨家数 - 下跌家数（TDX 统计口径） */
    breadth_net?: number | null;
    breadth_net_prev?: number | null;
    stopped_count?: number | null;
    avg_price?: number | null;
    note: string;
  };
  indices: Array<{ code: string; name: string; last?: number; change_pct?: number; amount?: number }>;
  breadth_trend?: { code: string; name: string; series: Array<{ date: string; value: number }> } | null;
  /** 上证 + 深证成交额求和；任一缺失则为 null（UI 显示「—」） */
  two_city_turnover: number | null;
  two_city_note: string;
  source: string;
  ts: string;
}

export interface MarketSourcesResponse {
  sources: string[];
  source_capabilities?: Record<string, unknown>;
  auto_chain: string[];
  health: Record<string, { available: boolean; note?: string }>;
  active: string | null;
}

export interface PeriodSpec {
  key: string;
  label: string;
  supported: boolean;
  reason?: string;
  kind?: string;
  [k: string]: unknown;
}

export interface LimitUpRow {
  code: string;
  name?: string;
  last?: number;
  change_pct?: number;
  amount?: number;
  [k: string]: unknown;
}

export interface LimitUpScanResponse {
  sector: string;
  count: number;
  rows: LimitUpRow[];
}

export interface L2Transaction {
  time?: string;
  price?: number;
  volume?: number;
  side?: string;
  [k: string]: unknown;
}

/**
 * 行情域 API。路径与 backend/app/routes/market.py 一一对应。
 *
 * ★ 契约修正（相对初版，逐端点核对后）：
 *   - kline 返回 { bars: [...] } 对象，**不是**裸数组
 *   - quotes 返回 { items: [...] } 对象，**不是**裸数组
 *   - periods 返回 { periods: [...] } 对象
 *   - kline 的参数名是 adj（不是 adjust）
 */
export const marketApi = {
  quote: (code: string, connId = "", source = "auto") =>
    http.get<Quote>("/market/quote", { query: { code, conn_id: connId, source } }),

  /** 批量快照。优先命中 SyncEngine 已订阅缓存，缺失项再打源补齐 */
  quotes: (codes: string[], connId = "", source = "auto") =>
    http.post<QuotesResponse>("/market/quotes", { codes, conn_id: connId, source }),

  /**
   * 历史 K 线。
   * adj: ""=不复权 / qfq=前复权 / hfq=后复权。
   * ⚠ 后端不支持按结束日期分页，故只能取「最近 count 根」；
   *   「滚动加载更早历史」需后端补 end 参数，当前不做假实现。
   */
  kline: (
    code: string,
    period: Period = "1d",
    count = 250,
    adj = "",
    connId = "",
    source = "auto",
    force = false,
  ) =>
    http.get<KlineResponse>("/market/kline", {
      query: { code, period, count, adj, conn_id: connId, source, force },
    }),

  /** 当日/历史分时（1 分钟）。date 为空取最新交易日 */
  minutes: (code: string, date = "", source = "auto") =>
    http.get<MinutesResponse>("/market/minutes", { query: { code, date, source } }),

  stockInfo: (code: string, connId = "", source = "auto") =>
    http.get<StockInfo>("/market/stock-info", { query: { code, conn_id: connId, source } }),

  /** 标的深度画像：单请求聚合 6 维（快照/画像/股本/表现/资金流/估值） */
  analysis: (code: string, connId = "", source = "auto") =>
    http.get<Record<string, unknown>>("/market/analysis", {
      query: { code, conn_id: connId, source },
    }),

  search: (q: string, limit = 20, includeBoards = true) =>
    http.get<Instrument[]>("/market/search", { query: { q, limit, include_boards: includeBoards } }),

  resolve: (q: string, limit = 8) =>
    http.get<Instrument[]>("/market/resolve", { query: { q, limit } }),

  indices: (codes = "", source = "auto", ttl = 3, spark = false, sparkDays = 20) =>
    http.get<IndicesResponse>("/market/indices", {
      query: { codes, source, ttl, spark, spark_days: sparkDays },
    }),

  boards: (kind = "industry", sortBy = "pct", limit = 50, source = "auto", ttl = 10) =>
    http.get<BoardsResponse>("/market/boards", {
      query: { kind, sort_by: sortBy, limit, source, ttl },
    }),

  boardConstituents: (code: string, limit = 100, page = 0, source = "auto", ttl = 15) =>
    http.get<BoardConstituentsResponse>("/market/board/constituents", {
      query: { code, limit, page, source, ttl },
    }),

  boardLookup: (name: string, limit = 8, source = "auto", ttl = 600) =>
    http.get<BoardLookupResponse>("/market/board/lookup", {
      query: { name, limit, source, ttl },
    }),

  boardKline: (code: string, period: Period = "1d", count = 60, source = "auto", ttl = 60) =>
    http.get<BoardKlineResponse>("/market/board/kline", {
      query: { code, period, count, source, ttl },
    }),

  etfs: (limit = 0, withQuote = false, source = "auto", ttl = 300) =>
    http.get<EtfResponse>("/market/etfs", {
      query: { limit, with_quote: withQuote, source, ttl },
    }),

  moneyflow: (code: string, source = "auto") =>
    http.get<MoneyflowResponse>("/market/moneyflow", { query: { code, source } }),

  boardMoneyflow: (code: string, source = "auto", topN = 30, ttl = 30) =>
    http.get<BoardMoneyflowResponse>("/market/board/moneyflow", {
      query: { code, source, top_n: topN, ttl },
    }),

  /** 板块轮动矩阵（热力图数据源） */
  rotation: (days = 5, kind = "industry", topN = 40, source = "auto") =>
    http.get<RotationResponse>("/market/rotation", {
      query: { days, kind, top_n: topN, source },
    }),

  /** 市场概览：广度 + 指数 + 宽度趋势 + 两市成交额 */
  overview: (source = "auto", ttl = 10) =>
    http.get<OverviewResponse>("/market/overview", { query: { source, ttl } }),

  /** 批量流通股本 + 涨跌停价（换手率/涨跌停展示的真实口径来源） */
  capital: (codes: string[], source = "auto", ttl = 300) =>
    http.get<{ shares: Record<string, unknown>; limits: Record<string, unknown> }>(
      "/market/capital",
      { query: { codes: codes.join(","), source, ttl } },
    ),

  /** 涨停板扫描（基于板块内最新行情） */
  limitupScan: (sector = "沪深A股", minPct = 9.5, onlyLimit = true, limit = 200, sort = "change") =>
    http.get<LimitUpScanResponse>("/market/limitup", {
      query: { sector, min_pct: minPct, only_limit: onlyLimit, limit, sort },
    }),

  /** 市场广度：全市场/板块/主要指数涨跌停家数 */
  breadth: () => http.get<Record<string, unknown>>("/market/breadth"),

  /** L2 逐笔成交 */
  l2: (code: string, count = 100) =>
    http.get<L2Transaction[]>("/market/l2", { query: { code, count } }),

  sources: () => http.get<MarketSourcesResponse>("/market/sources"),

  /** 可用周期清单（契约驱动 UI，supported=false 的周期应置灰） */
  periods: () => http.get<{ periods: PeriodSpec[] }>("/market/periods"),

  providers: () => http.get<Record<string, unknown>>("/market/providers"),

  klineSyncStatus: () => http.get<Record<string, unknown>>("/market/kline/sync-status"),

  klineCacheStats: () => http.get<Record<string, unknown>>("/market/kline/cache"),
};
