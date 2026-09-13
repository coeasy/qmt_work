// 仪表盘（v2 增强版）：市场温度计 + 指数矩阵 + 账户概览 + 系统健康 + 净值曲线。
// 参照东方财富首页布局，融合通达信终端风格。
import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import { useSystemStatus, useServerEvents } from "../hooks/useSystemWS.js";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
import { useQuotes } from "../lib/quoteHub.jsx";
import { WS_EVENTS } from "../shared/events";
import Chart from "./Chart.jsx";
import { t } from "../lib/i18n.js";
import { emit } from "../lib/eventBus";
import { navToQuote } from "../lib/nav.js";
import { fmtAmount } from "../lib/format.js";

const INDEX_CODES = ["000001", "399001", "399006", "000688"];
const INDEX_NAMES = { "000001": "上证指数", "399001": "深证成指", "399006": "创业板指", "000688": "科创50" };

export default function Dashboard() {
  const { activeId, activeBroker } = useBroker();
  const [status, setStatus] = useState(null);
  const [pnl, setPnl] = useState([]);
  const [err, setErr] = useState("");
  const [noBroker, setNoBroker] = useState(false);
  const [health, setHealth] = useState(null);
  const [agg, setAgg] = useState(null);
  const [slip, setSlip] = useState(null);
  const [overview, setOverview] = useState(null); // 市场全景（涨跌家数等）
  const { status: sysStatus, sys, latency } = useSystemStatus();
  const [rt, setRt] = useState(null);
  const loadRt = () => api.getRuntimeConfig().then(setRt).catch(() => {});
  useActiveInterval(loadRt, 15000);

  // 指数行情
  const { quotes: indexQuotes } = useQuotes(INDEX_CODES);

  async function loadHealth() {
    try { setHealth(await api.health()); } catch {}
  }
  useEffect(() => { loadHealth(); }, []);
  const loadAgg = () => api.aggregate().then(setAgg).catch(() => {});
  useActiveInterval(loadAgg, 30000, [activeId]);

  // 市场全景（涨跌家数/涨停跌停）
  const loadOverview = () => api.marketOverview().then(setOverview).catch(() => {});
  useActiveInterval(loadOverview, 15000);

  async function load() {
    try {
      const s = await api.get("/account/status", { conn_id: activeId });
      setStatus(s); setNoBroker(false);
      const p = await api.get("/account/pnl");
      setPnl(p.net_value_series || []);
      setErr("");
    } catch (e) {
      if (/未连接/.test(e.message)) setNoBroker(true);
      else setErr(e.message);
    }
  }
  useActiveInterval(load, 30000, [activeId]);
  useServerEvents([WS_EVENTS.ACCOUNT, WS_EVENTS.ORDER, WS_EVENTS.DEAL, WS_EVENTS.TRADE, "reconcile"], () => { load(); api.aggregate().then(setAgg).catch(() => {}); });
  useActiveInterval(() => { api.accountSlippage("600519.SH", activeId).then(setSlip).catch(() => {}); }, 60000, [activeId]);

  const pnlOption = {
    backgroundColor: "transparent",
    grid: { left: 50, right: 16, top: 20, bottom: 30 },
    xAxis: { type: "category", data: pnl.map((d) => d.ts), axisLabel: { color: "#8a97ad", fontSize: 10 } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#2c3850" } } },
    tooltip: { trigger: "axis" },
    series: [{
      type: "line", smooth: true, data: pnl.map((d) => d.net_value),
      areaStyle: { color: "rgba(79,140,255,.18)" }, lineStyle: { color: "#4f8cff" }, itemStyle: { color: "#4f8cff" },
    }],
  };

  const positions = (status?.positions || []);
  // 后端真实口径：TDX 统计板块返回「涨跌家数差」与「停板家数」，不提供上涨/下跌拆分。
  // 此前读取不存在的 up_count/down_count/limit_up_count 会恒定显示 0，像数据故障。
  const breadthSummary = overview?.breadth_summary || {};
  const breadthNet = breadthSummary.breadth_net ?? null;
  const breadthNetPrev = breadthSummary.breadth_net_prev ?? null;
  const breadthChange = (breadthNet != null && breadthNetPrev != null)
    ? breadthNet - breadthNetPrev : null;
  const stoppedCount = breadthSummary.stopped_count ?? null;
  const avgPrice = breadthSummary.avg_price ?? null;

  return (
    <div>
      <h2 className="page-title">{t(`page.dashboard.title`)}</h2>
      <p className="page-sub">
        市场全景与账户总览（事件驱动 · 兜底 30s 刷新）
        {activeBroker && <span className="muted"> · 当前连接：{activeBroker.broker_name} · {activeBroker.account_id || "—"}</span>}
      </p>
      {err && <div className="toast err">{err}</div>}

      {/* ===== 市场温度计 ===== */}
      <div className="card" style={{ marginTop: 12 }}>
        <h3>市场广度 <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>涨跌家数差 · 停板家数 · 成交均价</span></h3>
        <div style={{ display: "flex", alignItems: "center", gap: 20, marginTop: 8, flexWrap: "wrap" }}>
          {breadthNet == null ? <span className="muted">市场广度暂不可用（TDX 统计源未返回）</span> : (
            <div style={{ textAlign: "center", minWidth: 140 }}>
              <div style={{ fontSize: 24, fontWeight: 700, color: breadthNet >= 0 ? "var(--up)" : "var(--down)" }}>
                {breadthNet >= 0 ? "+" : ""}{breadthNet}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-dim)" }}>涨跌家数差</div>
            </div>
          )}
          {breadthChange != null && (
            <div style={{ textAlign: "center", minWidth: 100 }}>
              <div style={{ fontSize: 16, fontWeight: 600, color: breadthChange >= 0 ? "var(--up)" : "var(--down)" }}>
                {breadthChange >= 0 ? "+" : ""}{breadthChange}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-dim)" }}>较昨日同期</div>
            </div>
          )}
          <div style={{ textAlign: "center", minWidth: 90 }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: "var(--warn)" }}>
              {stoppedCount ?? "—"}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-dim)" }}>停板家数</div>
          </div>
          <div style={{ textAlign: "center", minWidth: 90 }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: "var(--text)" }}>
              {avgPrice != null ? Number(avgPrice).toFixed(2) : "—"}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-dim)" }}>全市场均价</div>
          </div>
          <span className="muted" style={{ fontSize: 11 }}>涨跌家数差 = 上涨家数 - 下跌家数；停板家数含涨跌停合计</span>
        </div>
      </div>

      {/* ===== 指数矩阵 ===== */}
      <div className="grid grid-4" style={{ marginTop: 12 }}>
        {INDEX_CODES.map((code) => {
          const q = indexQuotes[code];
          const last = q?.last != null ? Number(q.last) : null;
          const pct = q?.pct != null ? Number(q.pct) : (q?.pre_close && last ? ((last - q.pre_close) / q.pre_close) * 100 : null);
          const color = pct != null ? (pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--text)") : "var(--text)";
          return (
            <div key={code} className="card" style={{ cursor: "pointer" }} onClick={() => navToQuote(code)}>
              <h3 style={{ display: "flex", justifyContent: "space-between" }}>
                <span>{INDEX_NAMES[code] || code}</span>
                <span className="code">{code}</span>
              </h3>
              <div style={{ fontSize: 24, fontWeight: 700, color }}>
                {last != null ? last.toFixed(2) : "--"}
              </div>
              <div style={{ fontSize: 13, color, fontWeight: 600 }}>
                {pct != null ? `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%` : "--"}
              </div>
              {q?.amount && <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 4 }}>成交额 {fmtAmount(q.amount)}</div>}
            </div>
          );
        })}
      </div>

      {/* ===== 账户概览 ===== */}
      {health && (
        <div className="row" style={{ marginBottom: 12, marginTop: 12 }}>
          <span className={`tag ${health.status === "ok" || health.status === "pass" ? "ok" : "warn"}`}>
            系统：{health.status === "ok" || health.status === "pass" ? "健康" : "降级"} · v{health.version} · 运行 {Math.floor(health.uptime_seconds / 60)} 分钟
          </span>
          <span className={`tag ${health.db ? "ok" : "fail"}`}>DB {health.db ? "正常" : "异常"}</span>
          <span className="muted">已连接券商 {health.brokers?.filter((b) => b.connected).length ?? 0} 个</span>
        </div>
      )}
      <SystemMonitor status={sysStatus} sys={sys} latency={latency} rt={rt} />

      {noBroker && (
        <div className="empty-state">
          <p>尚未连接券商客户端。所有账户 / 行情 / 交易均依赖真实券商 SDK。</p>
          <button onClick={() => emit("nav", "brokers")}>前往「券商连接」添加并连接</button>
        </div>
      )}

      <div className="grid grid-4">
        <div className="card"><h3>总资产</h3><div className="stat-value">¥{(status?.assets ?? 0).toLocaleString()}</div></div>
        <div className="card"><h3>可用资金</h3><div className="stat-value">¥{(status?.cash ?? 0).toLocaleString()}</div></div>
        <div className="card"><h3>持仓数</h3><div className="stat-value">{status?.position_count ?? 0}</div></div>
        <div className="card"><h3>连接状态</h3><div className="stat-value" style={{ fontSize: 18 }}>
          <span className={status?.connected ? "tag ok" : "tag fail"}>{status?.connected ? "已连接" : "未连接"}</span>
        </div></div>
      </div>

      {agg && agg.account_count > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>多账户聚合（{agg.account_count} 个账户）</h3>
          <div className="grid grid-4">
            <div className="stat-value">¥{agg.total_assets.toLocaleString()}</div>
            <div className="stat-value">¥{agg.total_cash.toLocaleString()}</div>
            <div className="stat-value">¥{agg.total_market_value.toLocaleString()}</div>
            <div className="muted">委托 {agg.orders_count} · 成交 {agg.deals_count}</div>
          </div>
          <table style={{ marginTop: 10 }}>
            <thead><tr><th>券商</th><th>账户</th><th>总资产</th><th>可用</th><th>持仓市值</th><th>持仓数</th></tr></thead>
            <tbody>
              {agg.accounts.map((a, i) => (
                <tr key={i}><td>{a.broker}</td><td className="code">{a.account_id || a.name}</td>
                  <td>¥{a.assets.toLocaleString()}</td><td>¥{a.cash.toLocaleString()}</td>
                  <td>¥{a.market_value.toLocaleString()}</td><td>{a.position_count}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h3>净值曲线</h3>
        <Chart option={pnlOption} height={300} />
      </div>

      {slip && slip.samples && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>滑点分析（{slip.code}） <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>成交价 vs 当日 open/close/VWAP 基点差 · 平均 |滑点| {slip.avg_abs_slippage_avg_bps ?? "—"} bps</span></h3>
          {slip.samples.length === 0 ? <p className="muted">该标的近期无成交明细。</p> : (
            <div style={{ maxHeight: 260, overflow: "auto" }}>
              <table>
                <thead><tr><th>时间</th><th>方向</th><th>成交价</th><th>open(bps)</th><th>close(bps)</th><th>VWAP(bps)</th></tr></thead>
                <tbody>
                  {slip.samples.slice(0, 20).map((r, i) => (
                    <tr key={i}>
                      <td className="code">{r.time}</td><td>{r.side}</td><td>{r.price}</td>
                      <td>{r.slippage_open_bps}</td><td>{r.slippage_close_bps}</td><td>{r.slippage_avg_bps}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h3>持仓明细</h3>
        {positions.length === 0 ? <p className="muted">暂无持仓</p> : (
          <table>
            <thead><tr><th>代码</th><th>数量</th><th>市值</th><th>成本</th></tr></thead>
            <tbody>
              {positions.map((p, i) => (
                <tr key={i}><td>{p.code}</td><td>{p.volume}</td><td>¥{p.market_value?.toLocaleString()}</td><td>¥{p.cost}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function SystemMonitor({ status, sys, latency, rt }) {
  const ts = sys?.trading_session || {};
  const st = status === "connected" ? "ok" : status === "reconnecting" ? "warn" : "fail";
  const stLabel = status === "connected" ? "已连接"
    : status === "reconnecting" ? "重连中" : status === "offline" ? "离线" : "连接中";
  const uptime = sys?.uptime_seconds ? Math.floor(sys.uptime_seconds / 60) : 0;
  const engines = rt ? Object.entries(rt).map(([k, v]) => ({ k, v: v.value })) : [];
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <h3>系统监控 <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>（实时）</span></h3>
      <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <span className={`tag ${st}`}>WS {stLabel}{status === "connected" && latency != null ? ` · ${latency}ms` : ""}</span>
        {sys?.version && <span className="tag">v{sys.version}</span>}
        <span className="muted">运行 {uptime} 分钟</span>
        <span className="muted">券商 {sys?.brokers_connected ?? 0}/{sys?.brokers_total ?? 0}</span>
        <span className="muted">WS 客户端 {sys?.clients ?? 0}</span>
        <span className={`tag ${ts.active_now ? "run" : ""}`}>
          交易时段：{ts.active_now ? "盘中活跃" : "休眠"}（{ts.mode || "—"}）
        </span>
      </div>
      {rt && engines.length > 0 && (
        <div className="row" style={{ marginTop: 10, flexWrap: "wrap", gap: 6 }}>
          {engines.map((e) => (
            <span key={e.k} className="tag" title={e.k}>{e.k.split(".").pop()} = {e.v}</span>
          ))}
        </div>
      )}
    </div>
  );
}
