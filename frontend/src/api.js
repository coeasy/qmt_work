// 统一 REST 客户端：返回 {code,message,data} 中的 data，非 0 抛错。
const BASE = "/api/v1";

async function _req(method, path, { params, body } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  const r = await fetch(url.toString(), {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (j.code !== 0) throw new Error(j.message || `HTTP ${r.status}`);
  return j.data;
}

export const api = {
  get: (path, params) => _req("GET", path, { params }),
  post: (path, body) => _req("POST", path, { body }),
  put: (path, body) => _req("PUT", path, { body }),
  del: (path) => _req("DELETE", path),
};

// ---------------- 券商连接管理（多券商 / 多客户端版本）----------------
// 后端 BrokerManager 是唯一真相来源；前端为纯 SPA，仅透传 conn_id / broker_id。
api.brokerProfiles = () => api.get("/brokers/profiles");
api.listBrokers = () => api.get("/brokers");
api.addBroker = (body) => api.post("/brokers", body);
api.testBroker = (body) => api.post("/brokers/test", body);
api.autoDetectBrokers = () => api.get("/brokers/auto-detect");
api.connectBroker = (id) => api.post(`/brokers/${id}/connect`);
api.disconnectBroker = (id) => api.post(`/brokers/${id}/disconnect`);
api.setActiveBroker = (id) => api.post(`/brokers/${id}/active`);
api.removeBroker = (id) => api.del(`/brokers/${id}`);

// ---------------- 涨停监控 / 打板助手 ----------------
api.limitupStatus = () => api.get("/limitup/status");
api.limitupPoolAdd = (code) => api.post("/limitup/pool", { code });
api.limitupPoolRemove = (code) => api.del(`/limitup/pool?code=${encodeURIComponent(code)}`);
api.limitupStart = (body) => api.post("/limitup/start", body);
api.limitupStop = () => api.post("/limitup/stop");
api.limitupReset = () => api.post("/limitup/reset");

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

// ---------------- 策略模板库 ----------------
api.strategyGenerate = (body) => api.post("/strategies/generate", body);
api.strategySave = (body) => api.post("/strategies/save", body);

// ---------------- 工业级增强：健康检查 / 风控配置 / 审计 ----------------
api.health = () => api.get("/health");
api.getRiskConfig = () => api.get("/config/risk");
api.putRiskConfig = (body) => api.put("/config/risk", body);
api.audit = (params) => api.get("/audit", params);
api.aggregate = () => api.get("/account/aggregate");

// ---------------- 运行时配置中心（引擎参数热更新） ----------------
api.getRuntimeConfig = () => api.get("/config/runtime");
api.putRuntimeConfig = (body) => api.put("/config/runtime", body);
api.resetRuntimeConfig = (key) => api.post("/config/runtime/reset", { key });
api.riskDaily = () => api.get("/config/risk/daily");
api.riskCircuit = (body) => api.post("/config/risk/circuit", body);

// ---------------- 出站 webhook（B2） ----------------
api.webhooks = () => api.get("/webhooks");
api.saveWebhook = (body) => api.post("/webhooks", body);
api.deleteWebhook = (id) => api.del(`/webhooks/${id}`);
api.testWebhook = (id) => api.post(`/webhooks/${id}/test`);
api.webhookDeliveries = (sid) => api.get("/webhooks/deliveries", sid ? { sid } : {});

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

// Agent 对话：SSE 流式。回调 onEvent(evt) 收到解析后的事件对象。
export async function agentChat(message, onEvent, signal) {
  const r = await fetch(`${BASE}/agent/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const part of parts) {
      const lines = part.split("\n");
      let evt = "message";
      for (const ln of lines) {
        if (ln.startsWith("event:")) evt = ln.slice(6).trim();
        else if (ln.startsWith("data:")) {
          try {
            onEvent({ event: evt, data: JSON.parse(ln.slice(5).trim()) });
          } catch {
            /* ignore */
          }
        }
      }
    }
  }
}
