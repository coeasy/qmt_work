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

/**
 * 交易会话快照（`GET /market/session`，来源 `app/sync/calendar.py::session_snapshot`）。
 *
 * 存在的意义：非交易日后端照常返回**上一交易日**的数据（正确），但界面此前
 * 没有任何地方标注「这是哪天的行情」，用户会把周六看到的数字当成今日行情。
 */
export interface SessionSnapshot {
  /** 今天 YYYYMMDD */
  today: string;
  /** 今天是否交易日 */
  trading_day: boolean;
  /** 是否盘中活跃 */
  active: boolean;
  /** holiday / pre_open / open / lunch_break / closed */
  phase: string;
  /** 数据参照日（= 该看哪一天的行情），非交易日回退上一交易日 */
  last_trading_day: string;
  /** 同 last_trading_day，语义化别名 */
  as_of: string;
  /** 下一个交易日 */
  next_trading_day: string;
  /** 日历来源：exchange（券商真实日历，最准）/ builtin（内置节假日表） */
  calendar: { mode: string; exact: boolean };
  /** 服务端当前时刻 YYYY-MM-DD HH:MM:SS */
  now: string;
}

/** 会话阶段 → 中文标签（唯一入口，界面不得各自硬编码） */
export const SESSION_PHASE_LABEL: Record<string, string> = {
  holiday: "休市",
  pre_open: "盘前",
  open: "交易中",
  lunch_break: "午间休市",
  closed: "已收盘",
};

/** YYYYMMDD → YYYY-MM-DD（非 8 位数字原样返回） */
export function fmtBarDate(v: string | null | undefined): string {
  if (!v) return "";
  return /^\d{8}$/.test(v) ? `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)}` : v;
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
  /**
   * 行情派生字段（2026-09-21 扩展）。
   *
   * ⚠️ 后端给不出时是 **`null`**（不是 0）：这些是「源里没有」，不是「值为零」。
   * 前端一律渲染 `--`，**绝不能把 null 当 0 显示** —— 市值 0 元 / 市盈率 0
   * 会被读成真实数据（假数据比没数据更危险）。
   */
  open?: number | null;
  high?: number | null;
  low?: number | null;
  /** 均价（成交额 / 成交量） */
  avg_price?: number | null;
  /** 振幅 % */
  amplitude?: number | null;
  /** 换手率 % */
  turnover_rate?: number | null;
  /** 量比 */
  volume_ratio?: number | null;
  /** 市盈率 TTM（亏损股为负） */
  pe_ttm?: number | null;
  /** 市净率 */
  pb?: number | null;
  /** 流通市值（元） */
  circ_mv?: number | null;
  /** 总市值（元） */
  total_mv?: number | null;
  /** 成交额（元） */
  amount?: number | null;
  /**
   * 上面这些行情派生字段**实际来自哪个源**。
   *
   * 为什么需要：详情源（本地 TDX / 券商）不提供这些字段，后端会再从公开行情源补一次。
   * 此时 `source` 仍是详情源，不说明白就是「数据源写着 eltdx、市值却是别处来的」。
   * 与 `source` 相同、或没有补过时不出现。
   */
  metrics_source?: string | null;
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
  /**
   * 三块数据（统计板块 / 指数 / 宽度趋势）**全空**时后端给出的成因。
   *
   * 后端只在「确实拿不到」时才带上它；界面必须原样转述，不要自己编原因 ——
   * 「未连接券商」和「数据源不提供该能力」是两件事，猜错会把排查方向带偏。
   */
  unavailable?: string;
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
 * 一条**落库的**同步运行记录（`sync_state` 表）。
 *
 * ★ 与内存里的 `last_run` 的区别：内存版重启即丢，而用户判断「今天的数据到底
 * 同步了没有」恰恰是在重启之后。落库版还带 `detail`，能说清跑了几只、成功几只、
 * 数据截至哪天。`status` 四态：`ok` / `partial` / `error` / `skipped` ——
 * **skipped 不是失败**（如「热表内无日线序列」），但同样必须看得见。
 */
export interface SyncRunRecord {
  stream: string;
  /** 该流最近一次运行的时间戳（ISO） */
  last_ts?: string;
  status?: string;
  detail?: {
    /**
     * ⚠️ 这是**流的判别标签**（`"sync_bars"` / `"market.sync"`），
     * **不是**同步模式 —— 同步模式见 `sync_mode`。两者混用会把「全量回补」
     * 误判成普通增量同步。
     */
    mode?: string;
    /** 同步模式：`incremental`（默认，只补最近 N 根）/ `full`（向前翻页补齐历史） */
    sync_mode?: string;
    reason?: string;
    error?: string;
    summary?: string;
    date?: string;
    job_id?: string;
    codes?: number;
    ok?: number;
    fail?: number;
    total?: number;
    failed?: number;
    stale?: number;
    bars_written?: number;
    as_of_max?: string;
    elapsed_ms?: number;
    hot_days?: number;
    count_per_code?: number;
    /**
     * 全量回补：本次**是否真的按日期区间向前翻页**。
     * `mode === "full" && paged === false` ⇒ 当前数据源链没有支持区间取数的源
     * （免费在线源只接受 count），历史**并未**补齐 —— 界面必须如实说出来，
     * 否则「全量完成」是一句谎话。
     */
    paged?: boolean;
    /** 全量回补写入的最早一根 K 线日期（`YYYYMMDD`），判断历史推到了哪一年 */
    as_of_min?: string;
    /** 全量回补因「本地历史已覆盖目标起点」而跳过的标的数（断点续传的证据） */
    skipped_complete?: number;
    /** 降级原因（如「全量回补未生效」），有值即表示结果与名义模式不符 */
    degraded_reason?: string;
    errors?: Array<{ code?: string; error?: string }>;
    errors_truncated?: number;
    params?: Record<string, unknown>;
    [k: string]: unknown;
  };
}

/**
 * `/market/kline/sync-status` 返回体 —— 「每日定时下载 K 线 + 冷热分层」的观测面。
 *
 * 前端据此能如实告诉用户「今日已同步 / 待同步 / 未启用」以及冷热怎么分的，
 * 而不是只给一个开关（开关开着不代表今天跑过）。
 * `last_run` 只在**本进程**跑过之后才有值（重启后为空，属正常）；
 * 重启后要看历史请用 `last_run_persisted`。
 */
export interface KlineSyncStatus {
  initialized: boolean;
  enabled?: boolean;
  /** 触发时刻 `HH:MM`（默认 16:00） */
  sync_time?: string;
  /** runtime_config 的 domain key，供「设置」页写入同一份配置 */
  keys?: { enabled: string; sync_time: string; hot_days: string };
  last_run?: {
    date?: string;
    codes?: number;
    ok?: number;
    fail?: number;
    hot_days?: number;
    count_per_code?: number;
  } | null;
  /**
   * 落库版的上次运行（跨重启可查）—— **热窗口刷新流**（`market.sync`）。
   * `null` 表示**从未跑过** —— 与「跑过但失败」（`status: "error"`）是两件事。
   */
  last_run_persisted?: SyncRunRecord | null;
  /**
   * 落库版的**全市场日线同步**流（`sync.bars`）上次运行。
   *
   * ⚠️ 与 `last_run_persisted` 是**两条不同的流**，detail 结构也不同：
   * 只有这条才有 `sync_mode` / `paged` / `as_of_min` / `skipped_complete` /
   * `stale` / `as_of_max`。把全量回补的字段从 `last_run_persisted` 读会**永远
   * 读不到**（热刷新 detail 里根本没这些键），表现为「按钮点了没反应」。
   */
  last_bars_run?: SyncRunRecord | null;
  hot?: {
    /** 热窗口天数（自然日，默认 92 ≈ 3 个月） */
    hot_days?: number;
    /** 冷热边界 `YYYY-MM-DD`，早于它的为冷数据 */
    hot_cutoff?: string;
    hot_rows?: number;
    cold_rows?: number;
    cold_enabled?: boolean;
    cold_path?: string;
  };
}

/**
 * `/market/coverage` 返回体 —— 本地日线的**覆盖度报表**。
 *
 * ★ 这是「非空≠够新」这条硬约束唯一能让用户自查的出口：`last_run.ok=5221`
 * 只说明**调用**成功，不说明数据**新到哪天**。所以 `per_day` 如实返回
 * 「最近 N 个交易日每天在库多少只」，缺的日子**不出现**（不补 0 冒充）。
 */
export interface MarketCoverage {
  period: string;
  adjust: string;
  lookback_days: number;
  /** 每个交易日在库的 code 数，按 dt 降序；缺失的日子不在列表里 */
  per_day: Array<{ dt: string; codes: number }>;
  provider_share: Array<{ provider_id: string; bars: number; share: number }>;
  universe_size: number | null;
  /** 有数据的最新交易日（`""` = 一个都没有） */
  latest_day: string;
  latest_codes: number;
  days_with_data: number;
  /** `system.sync_bars` 的落库运行记录（`null` = 从未跑过） */
  sync: SyncRunRecord | null;
}

/** `/market/kline/cache` 返回体（KlineCache.stats 的口径） */
export interface KlineCacheStats {
  rows?: number;
  hot_rows?: number;
  archive_rows?: number;
  /** 热表 + 冷仓去重后的「代码|周期」序列数 */
  series?: number;
  hot_days?: number;
  hot_cutoff?: string;
  cold_enabled?: boolean;
  cold_path?: string;
  hits?: number;
  misses?: number;
  stale_serves?: number;
  /** 未命中过任何请求时为 null（不是 0 —— 0 会被误读成「全部未命中」） */
  hit_rate?: number | null;
  ttl_daily?: number;
  ttl_intraday?: number;
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
  /**
   * 交易会话快照：交易日 / 盘中阶段 / **数据参照日**（非交易日回退上一交易日）。
   *
   * 与 `/health.trading_session` 的分工：health 那份只有 mode/active/trading_day，
   * 用于状态栏角标（每 30s 轮询、字段极简）；本接口多给
   * `last_trading_day` / `as_of` / `phase` / `next_trading_day`，用于行情页头部
   * 「当前展示的是哪一天的数据」这类需要精确日期的展示，按需调用即可。
   */
  session: () => http.get<SessionSnapshot>("/market/session"),

  /**
   * 单标的快照。
   *
   * ⚠️ 目前**无调用方**（界面行情一律走 WS 订阅 + `stores/quotes.ts`）。
   * 重新启用前必须确认契约：eltdx 路径返回的是 `last` 而非界面契约名 `price`，
   * 直接用会得到一堆 `undefined` —— 与「订阅到了但没数字」是同一个坑。
   */
  quote: (code: string, connId = "", source = "auto") =>
    http.get<Quote>("/market/quote", { query: { code, conn_id: connId, source } }),

  /**
   * 批量快照。优先命中 SyncEngine 已订阅缓存，缺失项再打源补齐。
   *
   * ⚠️ 同 `quote`：当前无调用方；且 `/market/quotes` 在补齐失败时会**静默丢弃**
   * 缺项（`routes/market.py` 的 `except: return None`），只能靠 served/requested
   * 差值发现 —— 启用前需让调用方显式处理缺项。
   */
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

  /**
   * 市场概览：广度 + 指数 + 宽度趋势 + 两市成交额。
   *
   * ★ ttl 用 60（秒）而不是 10，理由是本页**唯一**新鲜度敏感的项是「指数最新价 /
   *   涨跌幅」，而它由 `MarketStructure` 用 `useLiveQuotes` 实时叠加（WS 推送），
   *   **不依赖**这个快照；其余各项（涨跌家数 / 停板家数 / 成交均价 / 宽度趋势 /
   *   两市成交额）都是慢变量。
   *   反过来，ttl=10 会让「切走页签再切回」几乎每次都重新打源 —— 实测本链路稳态
   *   2.4~4s、冷启动更久（见 `aggregates.overview` 的实测注释），等于每 10 秒让
   *   用户白等一次。页面另有「刷新」按钮可强制取新。
   */
  overview: (source = "auto", ttl = 60) =>
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

  klineSyncStatus: () => http.get<KlineSyncStatus>("/market/kline/sync-status"),

  klineCacheStats: () => http.get<KlineCacheStats>("/market/kline/cache"),

  /** 本地日线覆盖度报表（「数据到底新到哪天」的唯一自查出口） */
  coverage: (opts: { period?: string; adjust?: string; lookbackDays?: number } = {}) =>
    http.get<MarketCoverage>("/market/coverage", {
      query: {
        period: opts.period ?? "1d",
        adjust: opts.adjust ?? "qfq",
        lookback_days: opts.lookbackDays ?? 30,
      },
    }),
};
