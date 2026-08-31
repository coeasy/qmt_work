// 统一 REST 客户端：返回 {code,message,data} 中的 data，非 0 抛错。
const BASE = "/api/v1";

// 远程访问 API Key（localStorage 持久化；空 = 本机回环免鉴权，局域网/远程部署时填写）
const KEY_STORAGE = "qmt_api_key";
export function getApiKey() {
  try { return localStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
}
export function setApiKey(k) {
  try {
    if (k && k.trim()) localStorage.setItem(KEY_STORAGE, k.trim());
    else localStorage.removeItem(KEY_STORAGE);
  } catch { /* ignore */ }
}

function _authHeaders(extra = {}) {
  const key = getApiKey();
  if (key) return { ...extra, Authorization: `Bearer ${key}` };
  return extra;
}

async function _req(method, path, { params, body, signal, timeoutMs, _retried } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  // 阶段 5 关键修复：实现 signal + timeoutMs 真正生效（否则取消按钮无效、连接超时无意义）。
  // 双轨：外部 signal（用户点取消）+ 内部 timer，任一触发立即中断 fetch。
  // 链路强化：默认 15s 超时（未显式指定时），避免无超时请求永久挂起。
  const effTimeout = timeoutMs !== undefined ? timeoutMs : 15000;
  const ctrl = new AbortController();
  let timer = null;
  const onAbort = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener("abort", onAbort, { once: true });
  }
  if (effTimeout && effTimeout > 0) {
    timer = setTimeout(() => {
      try { ctrl.abort(new Error(`请求超时（${effTimeout}ms）`)); } catch { ctrl.abort(); }
    }, effTimeout);
  }
  let r;
  try {
    r = await fetch(url.toString(), {
      method,
      signal: ctrl.signal,
      headers: _authHeaders(body ? { "Content-Type": "application/json" } : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (timer) clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", onAbort);
    const aborted = signal && signal.aborted;
    if (aborted) throw new Error("已取消请求");
    // GET 幂等请求：网络层失败（断网/超时/DNS）自动重试一次（业务码错误不重试）。
    if (method === "GET" && !_retried) {
      await new Promise((res) => setTimeout(res, 400));
      return _req(method, path, { params, body, signal, timeoutMs, _retried: true });
    }
    throw new Error((e && e.message) || "网络请求失败");
  }
  if (timer) clearTimeout(timer);
  if (signal) signal.removeEventListener("abort", onAbort);
  const j = await r.json().catch(() => ({}));
  if (j.code !== 0) throw new Error(j.message || `HTTP ${r.status}`);
  return j.data;
}

export const api = {
  get: (path, params, opts) => _req("GET", path, { params, ...(opts || {}) }),
  post: (path, body, opts) => _req("POST", path, { body, ...(opts || {}) }),
  put: (path, body, opts) => _req("PUT", path, { body, ...(opts || {}) }),
  patch: (path, body, opts) => _req("PATCH", path, { body, ...(opts || {}) }),
  del: (path, opts) => _req("DELETE", path, opts || {}),
};

// ---------------- 券商连接管理（多券商 / 多客户端版本）----------------
// 后端 BrokerManager 是唯一真相来源；前端为纯 SPA，仅透传 conn_id / broker_id。
api.brokerProfiles = () => api.get("/brokers/profiles");
api.listBrokers = () => api.get("/brokers");
api.addBroker = (body) => api.post("/brokers", body);
api.testBroker = (body) => api.post("/brokers/test", body);
api.launchBrokerClient = (body) => api.post("/brokers/launch", body);
api.autoDetectBrokers = () => api.get("/brokers/auto-detect");
api.connectBroker = (id, { signal } = {}) =>
  _req("POST", `/brokers/${id}/connect`, { body: {}, signal, timeoutMs: 35000 });
api.disconnectBroker = (id) => api.post(`/brokers/${id}/disconnect`);
api.setActiveBroker = (id) => api.post(`/brokers/${id}/active`);
api.removeBroker = (id) => api.del(`/brokers/${id}`);
api.batchRemoveBrokers = (ids) => api.post("/brokers/batch-delete", { ids });
// ABI 运行时矩阵 / 单连接健康检查（运维排障）
api.brokerRuntimes = () => api.get("/brokers/runtimes");
api.brokerHealth = (id) => api.get(`/brokers/${id}/health`);
// QMT 客户端版本画像探测（识别完整版/极速版、版本号与能力矩阵）
api.brokerVersionInfo = (body) => api.post("/brokers/version-info", body);
// 端到端诊断快照（排障用；deep 含系统运行时发现，较重）
api.brokerDiagnostics = (deep) => api.get("/brokers/diagnostics", deep ? { deep: 1 } : {});

// ---------------- 涨停监控 / 打板助手 ----------------
api.limitupStatus = () => api.get("/limitup/status");
api.limitupPoolAdd = (code) => api.post("/limitup/pool", { code });
api.limitupPoolRemove = (code) => api.del(`/limitup/pool?code=${encodeURIComponent(code)}`);
api.limitupStart = (body) => api.post("/limitup/start", body);
api.limitupStop = () => api.post("/limitup/stop");
api.limitupReset = () => api.post("/limitup/reset");

// 涨停板（盘口扫描）：板块内涨停/接近涨停个股列表与最新数据
api.marketLimitup = (params) => api.get("/market/limitup", params);

// ---------------- 算法单（TWAP/VWAP）----------------
api.algoSubmit = (body) => api.post("/algo/submit", body);
api.algoList = () => api.get("/algo");
api.algoPause = (id) => api.post(`/algo/${id}/pause`);
api.algoResume = (id) => api.post(`/algo/${id}/resume`);
api.algoCancel = (id) => api.post(`/algo/${id}/cancel`);

// ---------------- 参考数据 / L2 ----------------
api.calendar = (start, end) => api.get("/reference/calendar", { start, end });
api.sectors = () => api.get("/reference/sectors");
api.sectorStocks = (sector) => api.get("/reference/sector-stocks", { sector });
api.financial = (code) => api.get("/reference/financial", { code });
api.l2 = (code, count) => api.get("/market/l2", { code, count });
api.marketKline = (params) => api.get("/market/kline", params);
api.marketMinutes = (params) => api.get("/market/minutes", params);
// G2-2 统一指标引擎：目录 + 服务端计算（前端不再自带指标实现，单一真源）
api.marketIndicators = () => api.get("/market/indicators");
api.marketIndicatorsCalc = (params) => api.get("/market/indicators/calc", params);
// G7 条件选股：conditions 为 JSON 条件树（URL 编码）；动态板块存取
api.marketScreen = (params) => api.get("/market/screen", params);
api.screenNL = (body) => api.post("/market/screen/nl", body);   // T5 G8 自然语言选股
api.screenBoards = () => api.get("/market/screen/boards");
api.screenBoardsSave = (body) => api.post("/market/screen/boards", body);
// G4 数据面策略表 + G9-4 图表规范（后端下发单一真源）
api.dataHubPolicies = () => api.get("/datahub/policies");
api.marketChartSpec = () => api.get("/market/chart-spec");
// 周期契约清单（契约驱动 UI）：前端周期条据此渲染，不支持的周期置灰并显示原因，
// 杜绝「点了月线实际出日线」这类前后端枚举漂移导致的静默错误。
api.marketPeriods = () => api.get("/market/periods");
// 行情工具：单票实时报价 / 手动抓取落库 / K 线缓存查看与清理
api.marketQuote = (params) => api.get("/market/quote", params);
api.marketQuotes = (body) => api.post("/market/quotes", body);
api.marketStockInfo = (params) => api.get("/market/stock-info", params);
// 股票搜索：按中文名/代码模糊匹配（后端零网络，基于本地名称缓存）
api.marketSearch = (q, limit = 20) => api.get("/market/search", { q, limit });
api.marketCrawl = (body) => api.post("/market/crawl", body);
api.klineCacheStats = () => api.get("/market/kline/cache");
api.klineCacheClear = (code, period) =>
  api.del(`/market/kline/cache?code=${encodeURIComponent(code || "")}&period=${encodeURIComponent(period || "")}`);
api.klineSyncStatus = () => api.get("/market/kline/sync-status");

// ---------------- 多维行情：指数 / 板块 / ETF / 资金流（东财对标，阶段 E/F/G/H） ----------------
// 主要指数聚合快照（顶部指数条 / 指数分析）
api.marketIndices = (params) => api.get("/market/indices", params);
// 板块榜单（真实板块指数 881/880 快照）：kind=industry|concept|stat，sort_by=pct|amount
api.marketBoards = (params) => api.get("/market/boards", params);
// 板块成分股（f10 实时涨跌幅/价格）
api.marketBoardConstituents = (params) => api.get("/market/board/constituents", params);
// 板块名称→代码精确匹配（个股页概念/行业深链稳化，避免 name.includes 误匹配/静默失败）
api.marketBoardLookup = (params) => api.get("/market/board/lookup", params);
// 板块/指数 K 线（kind=index）
api.marketBoardKline = (params) => api.get("/market/board/kline", params);
// ETF 全市场清单（代码段 51/56/58/15/16）；with_quote=true 附带实时快照
api.marketEtfs = (params) => api.get("/market/etfs", params);
// 个股资金流（真实内外盘口径）：inside/outside/net/strength/volume_ratio
api.marketMoneyflow = (params) => api.get("/market/moneyflow", params);
// 批量流通股本 + 涨跌停价（换手率与涨跌停展示的真实口径来源）
api.marketCapital = (params) => api.get("/market/capital", params);
// E3 市场概览：统计类板块真实家数 + 主要指数 + 宽度趋势
api.marketOverview = (params) => api.get("/market/overview", params);
// F4 板块轮动矩阵：topN 板块近 N 日每日%chg
api.marketRotation = (params) => api.get("/market/rotation", params);
// G2 板块资金流：成分股当日主力净流入聚合
api.boardMoneyflow = (params) => api.get("/market/board/moneyflow", params);
// G3 资金流回放
api.moneyflowReplay = (params) => api.get("/market/moneyflow/replay", params);

// ---------------- 策略模板库 ----------------
api.strategyGenerate = (body) => api.post("/strategies/generate", body);
api.strategySave = (body) => api.post("/strategies/save", body);

// ---------------- 策略运行容器（P0：在平台内把策略当作实盘/模拟机器人运行） ----------------
api.strategyRunList = () => api.get("/strategies/run");
api.strategyRunCreate = (body) => api.post("/strategies/run", body);
api.strategyRunGet = (id) => api.get(`/strategies/run/${id}`);
api.strategyRunStart = (id) => api.post(`/strategies/run/${id}/start`);
api.strategyRunStop = (id) => api.post(`/strategies/run/${id}/stop`);
api.strategyRunDelete = (id) => api.del(`/strategies/run/${id}`);
api.strategyRunBatchDelete = (ids) => api.post("/strategies/run/batch-delete", { ids });
api.strategyRunLogs = (id, limit) => api.get(`/strategies/run/${id}/logs`, limit ? { limit } : {});
api.strategyRunPrecheck = (body) => api.post("/strategies/run/precheck", body);

// ---------------- 风控预检（P1：非变更型，不计入日级计数） ----------------
api.tradePrecheck = (body) => api.post("/trade/precheck", body);

// ---------------- 工业级增强：健康检查 / 风控配置 / 审计 ----------------
api.health = () => api.get("/health");
api.getRiskConfig = () => api.get("/config/risk");
api.putRiskConfig = (body) => api.put("/config/risk", body);
api.audit = (params) => api.get("/audit", params);
api.auditVerify = () => api.get("/audit/verify");
api.aggregate = () => api.get("/account/aggregate");
// 滑点分析：成交价 vs 当日 open/close/vwap 基点差（需券商成交数据）
api.accountSlippage = (code = "600519.SH", connId = "") => api.get("/account/slippage", { code, conn_id: connId });

// ---------------- 运行时配置中心（引擎参数热更新） ----------------
api.getRuntimeConfig = () => api.get("/config/runtime");
api.putRuntimeConfig = (body) => api.put("/config/runtime", body);
api.runtimeHistory = () => api.get("/config/runtime/history");
api.runtimeRollback = (id) => api.post("/config/runtime/rollback", { id });
api.riskDaily = () => api.get("/config/risk/daily");
api.riskCircuit = (body) => api.post("/config/risk/circuit", body);

// ---------------- API Key 管理（列表/创建/轮换/编辑/删除） ----------------
api.createApiKey = (body) => api.post("/api-keys", body);
api.patchApiKey = (kid, body) => api.patch(`/api-keys/${kid}`, body);
api.rotateApiKey = (kid) => api.post(`/api-keys/${kid}/rotate`);
api.deleteApiKey = (kid) => api.del(`/api-keys/${kid}`);
api.batchDeleteApiKeys = (ids) => api.post("/api-keys/batch-delete", { ids });
api.cleanUnusedApiKeys = (days) => api.post("/api-keys/clean-unused", { days });

// ---------------- 回测任务删除 ----------------
api.backtestDeleteJob = (id) => api.del(`/backtest/jobs/${id}`);
api.backtestBatchDelete = (ids) => api.post("/backtest/jobs/batch-delete", { ids });

// ---------------- 多账户网格 / 批量操作 ----------------
api.accountGrid = () => api.get("/account/grid");
api.batchOrder = (body) => api.post("/account/batch/order", body);
api.batchCancel = (body) => api.post("/account/batch/cancel", body);
api.batchReconnect = (body) => api.post("/account/batch/reconnect", body);

// ---------------- 手动交易（Trade 页） ----------------
api.tradeOrder = (body) => api.post("/trade/order", body);
api.tradeCancel = (orderId) => api.post("/trade/cancel", { order_id: orderId });
api.tradePositions = (symbol) => api.get("/trade/positions", { symbol });
api.tradeOrders = () => api.get("/trade/orders");
api.tradeDeals = () => api.get("/trade/deals");
api.tradeTarget = (body) => api.post("/trade/target", body);
api.tradeConditions = () => api.get("/trade/conditions");
api.tradeConditionSubmit = (body) => api.post("/trade/conditions", body);
api.tradeConditionCancel = (cid) => api.post(`/trade/conditions/${cid}/cancel`);

// ---------------- 因子/指标库（P1，15 类指标） ----------------
api.computeManyFactors = (body) => api.post("/factors/compute/many", body);
api.factorFromKline = (body) => api.post("/factors/from-kline", body);

// ---------------- 研究深度层（阶段 3：因子IC/分位/组合回测/walk-forward/归因） ----------------
api.researchFactorIc = (body) => api.post("/research/factor-ic", body);
api.researchQuantile = (body) => api.post("/research/quantile", body);
api.researchCorrelation = (body) => api.post("/research/correlation", body);
api.researchPortfolioBacktest = (body) => api.post("/research/portfolio-backtest", body);
api.researchWalkForward = (body) => api.post("/research/walk-forward", body);
api.researchAttribution = (body) => api.post("/research/attribution", body);

// ---------------- 模拟盘（P1，实时真实行情 mark-to-market） ----------------
api.paperReset = () => api.post("/paper/reset");
api.paperOrder = (body) => api.post("/paper/order", body);
api.paperAccount = () => api.get("/paper/account");
api.paperPositions = () => api.get("/paper/positions");
api.paperTrades = () => api.get("/paper/trades");
api.paperMetrics = () => api.get("/paper/metrics");

// ---------------- 策略市场（P1，DB 目录 + zip/json 导入导出） ----------------
api.strategyCatalog = () => api.get("/strategy-market/catalog");
api.strategyMarketList = () => api.get("/strategy-market/market");
api.strategyPublish = (body) => api.post("/strategy-market/publish", body);
api.strategyInstall = (body) => api.post("/strategy-market/install", body);
api.strategyExport = (body) => api.post("/strategy-market/export", body);
api.strategyImport = (body) => api.post("/strategy-market/import", body);
api.strategyExportJson = (body) => api.post("/strategy-market/export-json", body);
api.strategyImportJson = (body) => api.post("/strategy-market/import-json", body);

// ---------------- 回测参数扫描（P1） ----------------
api.backtestSweep = (body) => api.post("/backtest/sweep", body);

// ---------------- 告警规则 ----------------
api.alertRules = () => api.get("/alerts/rules");
api.saveAlertRule = (body) => api.post("/alerts/rules", body);
api.deleteAlertRule = (id) => api.del(`/alerts/rules/${id}`);
api.batchDeleteAlertRules = (ids) => api.post("/alerts/rules/batch-delete", { ids });
api.testAlert = (body) => api.post("/alerts/test", body);
api.alertHistory = (limit) => api.get("/alerts/history", limit ? { limit } : {});

// ---------------- 出站 webhook ----------------
api.webhookSubscriptions = () => api.get("/webhooks");
api.webhookCreate = (body) => api.post("/webhooks", body);
api.webhookDelete = (sid) => api.del(`/webhooks/${sid}`);
api.webhookBatchDelete = (ids) => api.post("/webhooks/batch-delete", { ids });
api.webhookTest = (sid) => api.post(`/webhooks/${sid}/test`);
api.webhookDeliveries = () => api.get("/webhooks/deliveries");

// ---------------- 外部信号（Signal） ----------------
api.signalMode = () => api.get("/signal/mode");
api.signalSetMode = (body) => api.post("/signal/mode", body);
api.signalSubmit = (body) => api.post("/signal/submit", body);
api.signalConfirm = (body) => api.post("/signal/confirm", body);

// ---------------- 对账核销 / WAL ----------------
api.reconcileRun = (body) => api.post("/reconcile", body);
api.reconcileLast = () => api.get("/reconcile/last");
api.reconcileWalStats = () => api.get("/wal/stats");
api.reconcileWalCheckpoint = () => api.post("/wal/checkpoint");

// ---------------- 目标持仓 ----------------
api.targetSync = (body) => api.post("/target-portfolio/sync", body);
api.targetPlans = () => api.get("/target-portfolio/plans");
api.targetCreatePlan = (body) => api.post("/target-portfolio/plans", body);
api.targetDeletePlan = (pid) => api.del(`/target-portfolio/plans/${pid}`);
api.targetBatchDeletePlans = (ids) => api.post("/target-portfolio/plans/batch-delete", { ids });

// ---------------- 通知 ----------------
api.notifications = () => api.get("/notifications");
api.createNotification = (body) => api.post("/notifications", body);
api.deleteNotification = (nid) => api.del(`/notifications/${nid}`);
api.batchDeleteNotifications = (ids) => api.post("/notifications/batch-delete", { ids });
api.testNotification = () => api.post("/notifications/test");
api.notificationLogs = () => api.get("/notifications/logs");

// ---------------- 系统状态探针（live/ready/metrics/quote-bus） ----------------
api.live = () => api.get("/live");
api.ready = () => api.get("/ready");
api.quoteBusStats = () => api.get("/quote-bus/stats");
// 统一能力自描述：各域端点数 / 可自动暴露数（观察能力面是否完整）
api.capabilitiesSummary = () => api.get("/capabilities/summary");
// /metrics 返回 Prometheus text/plain（非 JSON），需原始文本读取
api.metricsRaw = async () => {
  const r = await fetch(`${BASE}/metrics`, { headers: _authHeaders() });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return await r.text();
};
