// 顶部券商连接条：纯展示 + 切换「当前操作连接」。切换即设为后端 active（实时行情随之）。
import { useBroker } from "../BrokerContext.jsx";

export default function BrokerBar() {
  const { brokers, activeId, activeBroker, connectedCount, setActive } = useBroker();

  return (
    <div className="broker-bar">
      <div className="broker-bar-left">
        <span className="muted">操作连接</span>
        {brokers.length === 0 ? (
          <span className="tag fail">未配置券商</span>
        ) : (
          <select
            className="broker-select"
            value={activeId}
            onChange={(e) => setActive(e.target.value)}
          >
            {brokers.map((b) => (
              <option key={b.conn_id} value={b.conn_id}>
                {b.broker_name} · {b.account_id || "—"} · {b.account_type}
                {b.connected ? " ✓" : " ✗"}
              </option>
            ))}
          </select>
        )}
        <span className={`tag ${activeBroker?.connected ? "ok" : "fail"}`}>
          {activeBroker?.connected ? "已连接" : (brokers.length ? "未连接" : "无客户端")}
        </span>
        {connectedCount > 1 && (
          <span className="muted">共 {connectedCount} 个连接在线</span>
        )}
      </div>
      <div className="broker-bar-right">
        <a className="link" href="#brokers" onClick={() => window.dispatchEvent(new CustomEvent("nav", { detail: "brokers" }))}>
          券商连接管理 →
        </a>
      </div>
    </div>
  );
}
