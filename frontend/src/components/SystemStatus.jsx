import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 系统状态（补齐 /live、/ready、/metrics、/quote-bus/stats 前端入口）
   聚合存活/就绪探针、行情总线统计与 Prometheus 原始指标，供排障与监控对接。 */

function fmtDuration(s) {
  s = Number(s) || 0;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${h}h ${m}m ${sec}s`;
}

function tagOf(status) {
  if (status === "pass" || status === "ok") return "ok";
  if (status === "warn") return "warn";
  return "fail";
}

export default function SystemStatus() {
  const [live, setLive] = useState(null);
  const [ready, setReady] = useState(null);
  const [readyErr, setReadyErr] = useState(null);
  const [bus, setBus] = useState(null);
  const [metrics, setMetrics] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => { loadAll(); const t = setInterval(loadAll, 15000); return () => clearInterval(t); },
    // eslint-disable-next-line
    []);

  async function loadAll() {
    setLoading(true);
    try {
      const lv = await api.live().catch(() => null);
      setLive(lv);
      // /ready 未就绪返回 HTTP 503（业务码 503），前端捕获后展示「未就绪」
      try { setReady(await api.ready()); setReadyErr(null); }
      catch (e) { setReady(null); setReadyErr(e.message); }
      const qb = await api.quoteBusStats().catch(() => null);
      setBus(qb);
      const mt = await api.metricsRaw().catch(() => "");
      setMetrics(mt || "");
    } finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>系统状态 <span style={{ fontSize: 13, color: "#8aa0c4" }}>存活 / 就绪 / 行情总线 / Prometheus</span></h2>
      </div>
      <div className="card">
        <button className="btn-sm" onClick={loadAll} disabled={loading} style={{ marginLeft: "auto" }}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      <div className="grid grid-2">
        {/* 存活 + 就绪 */}
        <div className="card">
          <h3>存活 / 就绪探针</h3>
          {live && (
            <div style={{ marginBottom: 10 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">存活</span>
                <span className={`tag ${live.status === "ok" ? "ok" : "fail"}`}>{live.status}</span>
              </div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">版本</span><span>{live.version}</span>
              </div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">运行时长</span><span>{fmtDuration(live.uptime_seconds)}</span>
              </div>
              <div style={{ marginTop: 6 }}>
                {(live.checks || []).map((c) => (
                  <span key={c.name} className={`tag ${tagOf(c.status)}`} style={{ marginRight: 6 }}>
                    {c.name}: {c.status}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div style={{ borderTop: "1px solid #22304a", paddingTop: 10 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="muted">就绪（DB 可读 + 引擎在跑）</span>
              {ready
                ? <span className="tag ok">就绪</span>
                : <span className="tag fail">未就绪</span>}
            </div>
            {ready && (
              <div style={{ marginTop: 6, fontSize: 12, lineHeight: 1.8 }}>
                <div className="row" style={{ justifyContent: "space-between" }}><span className="muted">DB</span><span>{String(ready.db)}</span></div>
                <div className="row" style={{ justifyContent: "space-between" }}><span className="muted">已启动</span><span>{String(ready.started)}</span></div>
                <div className="row" style={{ justifyContent: "space-between" }}><span className="muted">时长</span><span>{fmtDuration(ready.uptime_seconds)}</span></div>
                {(ready.engines && Object.entries(ready.engines)).map(([k, v]) => (
                  <div key={k} className="row" style={{ justifyContent: "space-between" }}>
                    <span className="muted">{k}</span><span>{String(v)}</span>
                  </div>
                ))}
              </div>
            )}
            {readyErr && <p className="muted" style={{ color: "#e6a23c", fontSize: 12 }}>未就绪：{readyErr}</p>}
          </div>
        </div>

        {/* 行情总线 */}
        <div className="card">
          <h3>行情总线统计</h3>
          {bus ? (
            <div style={{ fontSize: 13, lineHeight: 1.9 }}>
              <div style={{ fontWeight: 600 }}>总线模式：{bus.bus?.mode ?? "—"}</div>
              {bus.latency && (
                <div>延迟：{bus.latency.avg != null ? `${bus.latency.avg.toFixed(1)}ms` : "—"}
                  {bus.latency.p95 != null ? ` · p95 ${bus.latency.p95.toFixed(1)}ms` : ""}</div>
              )}
              <div>已订阅标的：<strong>{(bus.subscribed_codes || []).length}</strong> 个</div>
              <details style={{ marginTop: 6 }}>
                <summary style={{ cursor: "pointer", fontSize: 12, color: "#8aa0c4" }}>已订阅列表</summary>
                <div style={{ fontSize: 11, wordBreak: "break-all", maxHeight: 160, overflow: "auto" }}>
                  {(bus.subscribed_codes || []).join("、") || "无"}
                </div>
              </details>
              {bus.bus && Object.keys(bus.bus).length > 1 && (
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: "pointer", fontSize: 12, color: "#8aa0c4" }}>总线原始统计</summary>
                  <pre style={{ fontSize: 11, maxHeight: 180, overflow: "auto" }}>{JSON.stringify(bus.bus, null, 2)}</pre>
                </details>
              )}
            </div>
          ) : <p className="muted">暂无数据（行情总线未初始化或未连接券商）。</p>}
        </div>
      </div>

      {/* Prometheus 原始指标 */}
      <div className="card" style={{ marginTop: 12 }}>
        <h3>Prometheus 指标（原始）</h3>
        <pre style={{ fontSize: 11, maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
          {metrics || "（空）"}
        </pre>
      </div>
    </div>
  );
}
