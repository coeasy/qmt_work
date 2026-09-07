// ABI 运行时矩阵（排障：哪些券商 xtquant ABI 可被进程内直连/桥接覆盖）。
// 自 Brokers.jsx 原样搬移（行为零变更）；数据与加载动作由编排层传入。
export default function RuntimeMatrixCard({ runtimes, loadRuntimes }) {
  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>运行时矩阵</h3>
        <button className="btn-sm" onClick={loadRuntimes} style={{ marginLeft: "auto" }}>加载</button>
      </div>
      {runtimes == null && <p className="muted" style={{ marginTop: 8 }}>点击「加载」查看 ABI 运行时矩阵（主进程 Python / 桥接运行时 / 支持范围）。</p>}
      {runtimes?.error && <p className="muted" style={{ color: "#e6a23c" }}>加载失败：{runtimes.error}</p>}
      {runtimes && !runtimes.error && (
        <div style={{ marginTop: 8, fontSize: 13, lineHeight: 1.9 }}>
          <div>主进程 Python：<code>{runtimes.host_python}</code> · ABI <code>{runtimes.host_abi}</code></div>
          <div className="muted" style={{ fontSize: 11, margin: "4px 0" }}>{runtimes.supported_abi_note}</div>
          <div style={{ fontWeight: 600, marginTop: 6 }}>随包附带的桥接运行时</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
            {Object.entries(runtimes.bundled_runtimes || {}).map(([k, v]) => (
              <span key={k} className="tag">{v} · cp{k}</span>
            ))}
            {Object.keys(runtimes.bundled_runtimes || {}).length === 0 && (
              <span className="muted">无（全部进程内直连）</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
