// 状态条（TDX 风格 status bar，28px 高）：券商连接状态 / 行情 WS 状态 / 切换 / 快捷入口。
// 全部内容单行排布，避免换行。
import { useBroker } from "../BrokerContext.jsx";
import { useQuoteHub } from "../lib/quoteHub.jsx";
import { emit } from "../lib/eventBus";

const WS_LABEL = {
  connected: { txt: "行情实时", cls: "ok" },
  connecting: { txt: "行情连接中", cls: "warn" },
  reconnecting: { txt: "行情重连中", cls: "warn" },
  offline: { txt: "行情离线", cls: "fail" },
};

export default function BrokerBar() {
  const { brokers, activeId, activeBroker, connectedCount, setActive } = useBroker();
  const hub = useQuoteHub();
  const ws = WS_LABEL[hub.state] || WS_LABEL.offline;
  const nav = (k) => emit("nav", k);

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

      {/* 全局行情 WS 状态（v3 QuoteHub 单连接）：全站常驻可见，不再只藏在行情页 */}
      <div className="status-section" title={`行情 WebSocket：${ws.txt}（订阅 ${hub.declared} 只）`}>
        <span className={`status-dot ${ws.cls}`} aria-hidden="true" />
        <span className="status-text">{ws.txt}</span>
        {hub.declared > 0 && <span className="status-meta">订阅 {hub.declared}</span>}
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
