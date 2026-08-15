import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 可观测性（P2）
   Prometheus 指标 + 内存链路追踪环缓冲 + 运行时画像
*/

const TABS = [
  { key: "metrics", label: "指标" },
  { key: "traces", label: "链路追踪" },
  { key: "runtime", label: "运行时" },
];

export default function Observability() {
  const [tab, setTab] = useState("metrics");
  const [metrics, setMetrics] = useState(null);
  const [traces, setTraces] = useState([]);
  const [runtime, setRuntime] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { loadAll(); const t = setInterval(loadAll, 30000); return () => clearInterval(t); }, []);

  async function loadAll() {
    setLoading(true);
    try {
      const [m, tr, r] = await Promise.all([
        api.metricsSummary().catch(() => null),
        api.traces().catch(() => []),
        api.runtimeInfo().catch(() => null),
      ]);
      setMetrics(m); setTraces(tr || []); setRuntime(r);
    } finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>可观测性 <span style={{ fontSize: 13, color: "#8aa0c4" }}>Prometheus · 链路追踪 · 运行时</span></h2>
      </div>

      <div className="card">
        <div className="btn-group">
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
          <button className="btn-sm" onClick={loadAll} disabled={loading} style={{ marginLeft: "auto" }}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      {tab === "metrics" && <MetricsView metrics={metrics} />}
      {tab === "traces" && <TracesView traces={traces} />}
      {tab === "runtime" && <RuntimeView runtime={runtime} />}
    </div>
  );
}

function MetricsView({ metrics }) {
  if (!metrics) return <Empty>暂无指标</Empty>;
  const groups = metrics.groups || {};
  return (
    <div className="card">
      {Object.entries(groups).map(([group, items]) => (
        <div key={group} style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#8aa0c4", marginBottom: 8 }}>{group}</h4>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
            {items.map((m, i) => (
              <div key={i} className="metric-card">
                <div className="metric-name">{m.name}</div>
                <div className="metric-value">{formatVal(m.value)}</div>
                <div className="metric-help">{m.help || ""}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function TracesView({ traces }) {
  if (!traces.length) return <Empty>暂无链路追踪数据</Empty>;
  return (
    <div className="card">
      <div style={{ overflowX: "auto" }}>
        <table className="table">
          <thead><tr>
            <th>时间</th><th>操作</th><th>耗时(ms)</th><th>状态</th><th>详情</th>
          </tr></thead>
          <tbody>
            {traces.map((t, i) => (
              <tr key={i}>
                <td style={{ fontSize: 12, color: "#9bb" }}>{t.timestamp || ""}</td>
                <td><strong>{t.op || t.operation || t.name || ""}</strong></td>
                <td>{t.duration_ms != null ? t.duration_ms : "—"}</td>
                <td>
                  <span className={`tag ${t.status === "ok" ? "ok" : "error"}`}>
                    {t.status || "ok"}
                  </span>
                </td>
                <td style={{ fontSize: 11, color: "#9bb", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {t.detail || JSON.stringify(t.metadata || {})}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RuntimeView({ runtime }) {
  if (!runtime) return <Empty>暂无运行时信息</Empty>;
  const entries = Object.entries(runtime);
  return (
    <div className="card">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 12 }}>
        {entries.map(([k, v]) => (
          <div key={k} className="kv-pair">
            <span className="kv-label">{k}</span>
            <span className="kv-value">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "32px 24px", textAlign: "center", color: "#556" }}>{children}</div>;
}

function formatVal(v) {
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toFixed(2);
  }
  return String(v);
}