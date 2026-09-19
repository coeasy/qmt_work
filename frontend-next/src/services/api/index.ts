export { marketApi, SESSION_PHASE_LABEL, fmtBarDate } from "./market";
export type {
  BoardConstituentsResponse,
  BoardItem,
  BoardKlineResponse,
  BoardLookupResponse,
  BoardMoneyflowResponse,
  BoardsResponse,
  EtfResponse,
  IndicesResponse,
  KlineCacheStats,
  KlineResponse,
  KlineSyncStatus,
  LimitUpRow,
  LimitUpScanResponse,
  MarketSourcesResponse,
  MinutePoint,
  MinutesResponse,
  MoneyflowResponse,
  OverviewResponse,
  PeriodSpec,
  QuotesResponse,
  RotationBoard,
  RotationResponse,
  SessionSnapshot,
  StockInfo,
} from "./market";

export { tradeApi, signalApi } from "./trade";
export type {
  ConditionCreated,
  ConditionOrderPayload,
  OrderResult,
  PrecheckResult,
  SignalMode,
  SignalSubmitPayload,
  SubmitOrderPayload,
} from "./trade";

export { accountApi } from "./account";
export type { BatchBroadcast, BatchOrderItem } from "./account";

export { brokerApi } from "./broker";
export type {
  AutoDetectAccount,
  AutoDetectCandidate,
  AutoDetectResult,
  BrokerDiagnostics,
  BrokerTestResult,
  VersionProfile,
} from "./broker";

export {
  algoApi,
  limitupApi,
  alertApi,
  webhookApi,
  portfolioApi,
  reconcileApi,
} from "./automation";
export type { AlertRulePayload, AlgoSubmitPayload, LimitUpStartOptions } from "./automation";

export { systemApi, referenceApi, screenApi } from "./system";
export type {
  CapabilitiesResponse,
  CapabilitiesSummary,
  CapabilityItem,
  ClassicStrategy,
  HealthCheck,
  HealthResponse,
  LiveResponse,
  McpCapabilities,
  NlScreenResult,
  ReadyResponse,
  RuntimeJobSubmit,
  ScheduleCreate,
  ScreenResponse,
  ScreenRow,
  ScreenRunQuery,
} from "./system";

export { paperApi } from "./paper";
export type {
  PaperAccount,
  PaperMetrics,
  PaperOrderInput,
  PaperPosition,
  PaperTrade,
} from "./paper";

export { researchApi } from "./research";
export type {
  AttributionResponse,
  CorrelationResponse,
  FactorComputeResult,
  FactorInfo,
  FromKlineResult,
  IcResponse,
  IcStats,
  ManyFactorsResult,
  PortfolioBacktestResponse,
  PortfolioMetrics,
  QuantileResponse,
  QuantileRow,
  WalkForwardResponse,
} from "./research";
