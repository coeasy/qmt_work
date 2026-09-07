// 端到端诊断快照（浅层）：宿主 ABI / 桥接运行时 / 连接状态 / 行情泵健康。
// 自 Brokers.jsx 原样搬移（行为零变更）；数据与诊断动作由编排层传入。
export default function DiagnosticsPanel({ diag, diagBusy, loadDiagnostics }) {
  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>诊断快照</h3>
        <button className="btn-sm" onClick={loadDiagnostics} disabled={diagBusy} style={{ marginLeft: "auto" }}>
          {diagBusy ? "诊断中…" : "诊断"}
        </button>
      </div>
      {diag == null && !diagBusy && <p className="muted" style={{ marginTop: 8 }}>生成端到端诊断快照（不依赖真实券商连接），用于排障与长时段稳定性观察。</p>}
      {diag?.error && <p className="muted" style={{ color: "#e6a23c", marginTop: 8 }}>诊断失败：{diag.error}</p>}
      {diag && !diag.error && (
        <div style={{ marginTop: 8, fontSize: 13, lineHeight: 1.9 }}>
          <div>宿主 ABI：<code>{diag.host_abi}</code> · Python <code>{diag.host_python}</code></div>
          <div>桥接运行时：{Object.keys(diag.bundled_runtimes || {}).length
            ? Object.entries(diag.bundled_runtimes).map(([k, v]) => `${v} · cp${k}`).join("、")
            : "无（进程内直连）"}</div>
          <div className="muted" style={{ fontSize: 11 }}>生成于 {new Date((diag.generated_at || 0) * 1000).toLocaleTimeString()}</div>
          {(diag.connections || []).map((c) => (
            <div key={c.conn_id} style={{ borderTop: "1px solid #22304a", padding: "6px 0", fontSize: 12 }}>
              <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <span>{c.name} <span className="muted">[{c.broker_id}]</span></span>
                <span className={`tag ${c.connected ? "ok" : "fail"}`}>{c.connected ? "已连接" : "未连接"}</span>
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                适配 {c.adapter} · 健康 {c.health_status ?? "—"} · 重连 {c.reconnect_attempts ?? 0} 次 · 行情泵 {c.pump_running ? "运行中" : "停止"}
                {c.active ? " · 活跃" : ""}
              </div>
              {c.last_error && <div className="muted" style={{ fontSize: 11, color: "#e6a23c" }}>最近错误：{c.last_error}</div>}
            </div>
          ))}
          {(diag.connections || []).length === 0 && <p className="muted" style={{ marginTop: 6 }}>尚无已配置连接。</p>}
        </div>
      )}
    </div>
  );
}
