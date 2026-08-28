// 状态条（TDX 风格 status bar，28px 高）：券商连接状态 / 切换 / 快捷入口。
// 全部内容单行排布，避免换行。
import { useBroker } from "../BrokerContext.jsx";

export default function BrokerBar() {
  const { brokers, activeId, activeBroker, connectedCount, setActive } = useBroker();
  const nav = (k) => window.dispatchEvent(new CustomEvent("nav", { detail: k }));

  return (
    <div className="status-bar">
      <div className="status-section">
        <span className="status-label">连接</span>
        <span className={`status-dot ${activeBroker?.connected ? "ok" : "fail"}`} aria-hidden="true" />
        <span className="status-text">
          {brokers.length === 0
            ? "未配置券商"
            : (activeBroker?.connected ? "已连接" : (brokers.length ? "未连接" : "无客户端"))}
        </span>
        {brokers.length > 0 && (
          <select
            className="status-select"
            value={activeId}
            onChange={(e) => setActive(e.target.value)}
            title="切换当前操作连接"
          >
            {brokers.map((b) => (
              <option key={b.conn_id} value={b.conn_id}>
                {b.broker_name} · {b.account_id || "—"} · {b.connected ? "✓" : "✗"}
              </option>
            ))}
          </select>
        )}
        {connectedCount > 1 && <span className="status-meta">在线 {connectedCount}</span>}
      </div>

      <span className="status-sep" />

      <div className="status-section">
        <button className="status-link" onClick={() => nav("brokers")}>连接管理</button>
        <button className="status-link" onClick={() => nav("sysstatus")}>系统状态</button>
        <button className="status-link" onClick={() => nav("audit")}>审计</button>
      </div>

      <div className="status-section status-section-right">
        <span className="status-hint">Ctrl+K 命令面板 · F1 帮助</span>
      </div>
    </div>
  );
}
