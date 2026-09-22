import { http } from "../http";

/**
 * 因子 / 研究深度 API 层。
 *
 * ★ 口径来源：backend/app/routes/factors.py 与 research.py（逐文件核对，非猜测）。
 *   - GET  /factors                      列出全部可用指标（含默认参数）
 *   - POST /factors/compute              单指标计算（手动序列）
 *   - POST /factors/compute/many         批量指标计算
 *   - POST /factors/from-kline           基于真实券商 K 线计算（不伪造数据）
 *   - POST /research/factor-ic           因子 IC / ICIR
 *   - POST /research/quantile            分位分组
 *   - POST /research/correlation         因子相关性矩阵
 *   - POST /research/portfolio-backtest  组合回测
 *   - POST /research/walk-forward        walk-forward 滚动验证
 *   - POST /research/attribution         绩效归因（真实成交）
 *
 * 零 mock：未连接券商时后端返回 503，http.ts 原样抛 ApiError，本层不粉饰。
 */

export interface FactorInfo {
  name: string;
  desc?: string;
  label?: string;
  params?: Record<string, unknown>;
  default_params?: Record<string, unknown>;
}

export interface FactorComputeResult {
  name: string;
  values: number[];
}

export type ManyFactorsResult = Record<string, number[]>;

export interface FromKlineResult {
  symbol: string;
  period: string;
  count: number;
  source?: string;
  values: Record<string, number[]>;
}

export interface IcStats {
  ic_mean?: number;
  ic_std?: number;
  icir?: number;
  positive_ratio?: number;
  t_stat?: number;
  significant?: boolean;
}

export interface IcResponse {
  symbol?: string;
  symbols?: string[];
  factor_name: string;
  mode: string;
  method: string;
  forward: number;
  source?: string;
  ic?: number | null;
  ic_series?: (number | null)[];
  stats?: IcStats;
}

export interface QuantileRow {
  q: number;
  count: number;
  min: number;
  max: number;
  avg_return: number;
  cum_return: number;
}

export interface QuantileResponse {
  symbol: string;
  factor_name: string;
  forward: number;
  source?: string;
  long_short_avg_return?: number;
  long_short_sharpe?: number | null;
  quantiles?: QuantileRow[];
  spread_by_quantile?: number[];
}

export interface CorrelationResponse {
  factor_name?: string;
  method: string;
  names: string[];
  matrix: Record<string, Record<string, number | null>>;
}

export interface PortfolioMetrics {
  annual_return?: number;
  sharpe?: number;
  max_drawdown?: number;
}

/**
 * 分标的指标（tools/factor_research.run_portfolio_backtest：per_symbol_metrics 的值）。
 * 组合层 portfolio_metrics 不含 total_return，分标的才有——两者类型必须分开。
 */
export interface PortfolioSymbolMetrics extends PortfolioMetrics {
  total_return?: number;
}

export interface PortfolioBacktestResponse {
  source?: string;
  symbols: string[];
  weights: number[];
  portfolio_metrics: PortfolioMetrics;
  per_symbol_metrics: Record<string, PortfolioSymbolMetrics>;
  target_portfolio?: unknown;
  note?: string;
}

export interface WalkForwardFold {
  train_range: [string, string];
  test_range: [string, string];
  params: Record<string, unknown>;
  metrics: { sharpe?: number; total_return?: number };
}

export interface WalkForwardSummary {
  robustness?: string;
  n_folds?: number;
  test_sharpe_mean?: number;
  test_sharpe_std?: number;
  positive_folds?: number;
  param_drift?: Record<string, number[]>;
}

export interface WalkForwardResponse {
  summary?: WalkForwardSummary;
  folds?: WalkForwardFold[];
}

export interface AttributionResponse {
  total_pnl?: number;
  slippage?: { n?: number; avg_slippage_bps?: number | null };
  cost?: { commission_est?: number; stamp_tax_est?: number; total_est?: number };
  by_symbol?: Record<string, number>;
  by_side?: { buy: number; sell: number };
}

export const researchApi = {
  factorsList: () => http.get<FactorInfo[]>("/factors"),

  factorCompute: (body: { name: string; values: number[]; params?: Record<string, unknown> }) =>
    http.post<FactorComputeResult>("/factors/compute", body),

  computeManyFactors: (body: { names: string[]; values: number[]; params?: Record<string, unknown> }) =>
    http.post<ManyFactorsResult>("/factors/compute/many", body),

  factorFromKline: (body: {
    symbol: string;
    names: string[];
    period?: string;
    count?: number;
    broker_id?: string;
    params?: Record<string, unknown>;
  }) => http.post<FromKlineResult>("/factors/from-kline", body),

  factorIc: (body: Record<string, unknown>) => http.post<IcResponse>("/research/factor-ic", body),

  quantile: (body: Record<string, unknown>) => http.post<QuantileResponse>("/research/quantile", body),

  correlation: (body: Record<string, unknown>) =>
    http.post<CorrelationResponse>("/research/correlation", body),

  portfolioBacktest: (body: Record<string, unknown>) =>
    http.post<PortfolioBacktestResponse>("/research/portfolio-backtest", body),

  walkForward: (body: Record<string, unknown>) =>
    http.post<WalkForwardResponse>("/research/walk-forward", body),

  attribution: (body: Record<string, unknown>) =>
    http.post<AttributionResponse>("/research/attribution", body),
};

/* ---------------- 回测（口径来源：app/routes/backtest.py + backtest/__init__.py） ---------------- */

/**
 * 回测作业。
 *
 * ⚠️ 两种来源形状不同：**内存作业**给 `params` / `result` 对象；**DB 行**给
 * `params_json` / `result_json` 字符串（且 `result` 可能是字符串）。
 * 之前这里没有界面，于是「回测跑完没有」只能翻库 —— 页面必须同时吃下两种形状。
 */
export interface BacktestJob {
  id: string;
  kind?: string;
  status?: string;
  progress?: number;
  params?: unknown;
  params_json?: string;
  result?: unknown;
  result_json?: string;
  error?: string;
  created_at?: string;
  updated_at?: string;
  [k: string]: unknown;
}

/** tools/metrics.py::compute_metrics 的输出（字段可能缺省，故全部可选）。 */
export interface BacktestMetrics {
  total_return?: number;
  annual_return?: number;
  annual_volatility?: number;
  sharpe?: number;
  sortino?: number;
  max_drawdown?: number;
  calmar?: number | null;
  win_rate?: number;
  trade_count?: number;
  avg_pnl?: number;
  var95?: number | null;
  cvar95?: number | null;
  rating?: string;
  /** 样本不足时后端**主动声明**「VaR/CVaR 不具统计意义」，界面必须照显示 */
  tail_metrics_note?: string;
  profit_factor?: number | null;
  payoff_ratio?: number | null;
  avg_win?: number;
  avg_loss?: number;
  annualization?: number;
  period?: string;
  [k: string]: unknown;
}

export interface BacktestResult {
  id?: number;
  equity?: number[];
  trades?: Record<string, unknown>[];
  metrics?: BacktestMetrics;
  train?: unknown;
  test?: unknown;
  [k: string]: unknown;
}

export const backtestApi = {
  jobs: () => http.get<BacktestJob[]>("/backtest/jobs"),
  job: (id: string) => http.get<BacktestJob>(`/backtest/jobs/${id}`),
  /** kind: backtest / compare / sensitivity / sweep（后端白名单，写错 400） */
  submit: (body: { kind?: string; params: Record<string, unknown> }) =>
    http.post<BacktestJob>("/backtest/jobs", body),
  /** DELETE 单个 = 取消（只对 pending/running 有效，后端返回 cancelled:false 表示没取消掉） */
  cancel: (id: string) => http.del<{ cancelled: boolean }>(`/backtest/jobs/${id}`),
  batchDelete: (ids: string[]) =>
    http.post<{ deleted: number }>("/backtest/jobs/batch-delete", { ids }),
  sweep: (body: Record<string, unknown>) => http.post<BacktestJob>("/backtest/sweep", body),
};

/* ---------------- 策略市场（口径来源：app/routes/strategy_market.py） ---------------- */

/** 内置模板目录项（`tools/strategy_market.strategy_catalog`）。 */
export interface MarketCatalogItem {
  id: string;
  name?: string;
  type?: string;
  description?: string;
  params_schema?: unknown[];
  [k: string]: unknown;
}

/** 市场里的一条策略（`_row_to_dict` 会把 tags_json 解成 tags 数组）。 */
export interface MarketStrategy {
  id: string;
  title?: string;
  author?: string;
  description?: string;
  type?: string;
  tags?: string[];
  content?: string;
  created_at?: string;
  downloads?: number;
  [k: string]: unknown;
}

export const strategyMarketApi = {
  catalog: () => http.get<MarketCatalogItem[]>("/strategy-market/catalog"),
  list: (tag?: string, limit = 50) =>
    http.get<MarketStrategy[]>("/strategy-market/market", {
      query: { ...(tag ? { tag } : {}), limit },
    }),
  get: (id: string) => http.get<MarketStrategy>(`/strategy-market/market/${id}`),
  /** 发布：{strategy_id?, title, author?, description?, type, content, tags?} */
  publish: (body: Record<string, unknown>) =>
    http.post<MarketStrategy>("/strategy-market/publish", body),
  /** 安装到 QMT 客户端 mpython 目录：{id, client_path}（client_path 空 ⇒ 400） */
  install: (body: { id: string; client_path: string }) =>
    http.post<Record<string, unknown>>("/strategy-market/install", body),
  exportBundle: (body: { ids: string[]; path?: string }) =>
    http.post<Record<string, unknown>>("/strategy-market/export", body),
  importBundle: (body: { path: string }) =>
    http.post<Record<string, unknown>>("/strategy-market/import", body),
  exportJson: (body: { id: string; path?: string }) =>
    http.post<Record<string, unknown>>("/strategy-market/export-json", body),
  importJson: (body: { path: string }) =>
    http.post<Record<string, unknown>>("/strategy-market/import-json", body),
};
