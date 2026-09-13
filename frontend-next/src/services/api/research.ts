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
