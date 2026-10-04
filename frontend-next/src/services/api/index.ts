/**
 * 前端 API 客户端（REST）汇总出口。
 *
 * ## 客户端表面积纪律（R19 第 3 轮定案）
 *
 * **只声明会被调用的方法。** 端点仍然存在、但界面不打算接线时，不要在这里留一个
 * 「占位方法」——死声明有两个真实危害，都已在 R19-3 实测到：
 *
 * 1. **它会让能力门禁假绿**：`scripts/check_capability_coverage.py` 旧判据把
 *    「前端源码里出现过该路径」当作「界面有入口」，而路径字面量的唯一出现处通常
 *    就是这里的声明 ⇒ 一条没有任何调用点的死声明，足以把一个「界面上找不到」的
 *    后端能力判成「已覆盖」。R19-3 收紧为「声明不算入口、接线才算」后，一次性
 *    暴露出 **21 个**这样的端点。
 * 2. **它会腐烂**：路径/字段随后端漂移，而没有任何消费方会因为编译错误提醒你。
 *
 * 处置口径（与 R38 删除死代码 `referenceApi` 一致）：
 *   - **UI 不建域** 的端点 → 在 `check_capability_coverage.py` 的 `EXEMPT_PATHS`
 *     登记「仅 API/MCP」，前端不留声明；
 *   - **将来补了 UI** → 先删掉那条豁免，再加这里的方法（门禁会因「已接线」自然通过）。
 *
 * 说明：响应**类型**（如 `IndicesResponse` / `LiveResponse`）仍保留 —— 它们是后端
 * 载荷的契约文档，且编译期即被擦除、不产生运行时行为；删掉方法不等于要抹掉契约。
 */
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
  MarketCoverage,
  MarketSourcesResponse,
  MarketTick,
  MarketTicksResponse,
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
  SyncRunRecord,
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
  BigQmtProbe,
  BrokerDiagConnection,
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

export { systemApi, screenApi } from "./system";
export { pathsApi } from "./paths";
export type {
  BackupActionResult,
  BackupFileInfo,
  BackupPruneResult,
  BackupPruneResultFull,
  BackupRunResult,
  BackupStats,
  PathCandidate,
  PathDirInfo,
  PathKind,
  PathsMigrateResult,
  PathsResponse,
  PathsSetResult,
  PathValidateResult,
} from "./paths";
export type {
  CapabilitiesResponse,
  CapabilitiesSummary,
  CapabilityItem,
  ClassicPicksResponse,
  ClassicStrategy,
  DatahubPolicies,
  SourceDiagnostics,
  DatahubPolicy,
  DataProviderInfo,
  DataProvidersHealth,
  DataProvidersResponse,
  HealthCheck,
  HealthResponse,
  LiveResponse,
  McpCapabilities,
  NlScreenResult,
  NotificationConfig,
  NotificationPayload,
  ReadyResponse,
  RuntimeJobSubmit,
  ScheduleCreate,
  ScreenPick,
  ScreenResponse,
  ScreenRow,
  ScreenRunMeta,
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

export { researchApi, backtestApi, strategyMarketApi } from "./research";
export type {
  AttributionResponse,
  BacktestJob,
  BacktestMetrics,
  BacktestResult,
  MarketCatalogItem,
  MarketStrategy,
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
