import { useState } from "react";

/* 通用 Hub 容器：把一个领域下的多个页面合并到同一个顶层入口，
   内部用子页签切换。未激活的子组件会被卸载（停止其轮询/定时器）。
   - tabs: [{ key, label, comp }]，comp 为已渲染的 JSX 元素（如 <Trade />）
   - initial: 默认选中的子页签 key（用于外部 nav 事件直达，如 brokers → 连接管理）
*/
export default function Hub({ tabs, initial }) {
  const [active, setActive] = useState(initial || (tabs[0] && tabs[0].key));
  const cur = tabs.find((t) => t.key === active) || tabs[0];
  if (!cur) return null;
  return (
    <div className="hub">
      <div className="card hub-bar">
        <div className="btn-group hub-tabs">
          {tabs.map((t) => (
            <button
              key={t.key}
              className={t.key === active ? "active" : ""}
              onClick={() => setActive(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="hub-body">{cur.comp}</div>
    </div>
  );
}
