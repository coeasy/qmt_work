// 已配置连接列表卡片：批量删除条 + 连接表格（连接/断开/取消/设为活跃/健康检查/删除）。
// 自 Brokers.jsx 原样搬移（行为零变更）；状态与动作均由编排层通过 props 传入。
import BatchDeleteBar from "../BatchDeleteBar.jsx";

export default function ConnectionCard({
  brokers, bsel, batchDeleteBrokers, batchBusy,
  connecting, doConnect, cancelConnect,
  disconnect, setActive, remove,
  health, healthBusy, checkHealth,
  profile,
}) {
  return (
    <div className="card">
      <h3>已配置连接（{brokers.length}）</h3>
      <BatchDeleteBar count={bsel.selected.length} onDelete={batchDeleteBrokers} onClear={bsel.clear} busy={batchBusy} label="连接" />
      {brokers.length === 0 ? (
        <p className="muted">尚无连接。在左侧添加你的第一个券商客户端。</p>
      ) : (
        <table>
          <thead><tr>
            <th style={{ width: 32 }}><input type="checkbox" checked={bsel.allSelected} onChange={bsel.toggleAll} /></th>
            <th>名称</th><th>账户</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>
            {brokers.map((b) => (
              <tr key={b.conn_id}>
                <td><input type="checkbox" checked={bsel.sel.has(b.conn_id)} onChange={() => bsel.toggleOne(b.conn_id)} /></td>
                <td>
                  <div>{b.broker_name}</div>
                  <div className="muted" style={{ fontSize: 11 }}>
                    {b.adapter} · v{b.client_version || "?"} · {b.account_type}
                  </div>
                </td>
                <td>{b.account_id || "—"}</td>
                <td>
                  <span className={`tag ${b.connected ? "ok" : "fail"}`}>{b.connected ? "已连接" : "未连接"}</span>
                  {b.active && <span className="tag run" style={{ marginLeft: 6 }}>活跃</span>}
                </td>
                <td>
                  <div className="op-stack">
                    {b.connected ? (
                      <button className="ghost" onClick={() => disconnect(b.conn_id)}>断开</button>
                    ) : connecting[b.conn_id] ? (
                      <div className="op-stack" style={{ gap: 6 }}>
                        <button className="ghost" disabled>
                          <span className="spin">⟳</span> 连接中…
                          <span className="muted" style={{ fontSize: 11 }}>
                            {Math.max(0, Math.floor((Date.now() - connecting[b.conn_id].startedAt) / 1000))}s
                          </span>
                        </button>
                        <button className="ghost danger" onClick={() => cancelConnect(b.conn_id)}>取消</button>
                        <p className="muted" style={{ fontSize: 11, marginTop: 4, lineHeight: 1.5 }}>
                          {connecting[b.conn_id].hint}
                        </p>
                      </div>
                    ) : (
                      <button onClick={() => doConnect(b.conn_id)}>连接</button>
                    )}
                    {!b.active && !connecting[b.conn_id] && (
                      <button className="ghost" onClick={() => setActive(b.conn_id)}>设为活跃</button>
                    )}
                    <button className="ghost" onClick={() => checkHealth(b.conn_id)} disabled={healthBusy === b.conn_id}>
                      {healthBusy === b.conn_id ? "检查中…" : "健康检查"}
                    </button>
                    <button className="ghost danger" onClick={() => remove(b.conn_id)}>删除</button>
                  </div>
                  {health[b.conn_id] && (
                    <div className={`toast ${health[b.conn_id].ok ? "ok" : "err"}`} style={{ position: "static", marginTop: 6, fontSize: 12, whiteSpace: "pre-wrap" }}>
                      {health[b.conn_id].ok
                        ? `健康：连接正常 · ${JSON.stringify(health[b.conn_id].data).slice(0, 160)}`
                        : `不健康：${health[b.conn_id].err}`}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {profile && (
        <p className="muted" style={{ marginTop: 12 }}>
          支持周期：{profile.supported_periods.join(" / ")}
        </p>
      )}
    </div>
  );
}
