import { useState } from "react";

/* 通用 Hub 容器：把一个领域下的多个页面合并到同一个顶层入口，
   内部用子页签切换。未激活的子组件会被卸载（停止其轮询/定时器）。
   - tabs: [{ key, label, comp }]，comp 为组件引用（如 Trade）或工厂函数 () => JSX。
     传入组件引用时 Hub 自动实例化（避免在 NAV 静态定义中预创建 <LazyComp/> 元素，
     杜绝 Suspense + lazy 复用同一元素引用导致的 React #31 渲染崩溃）。
   - initial: 默认选中的子页签 key（用于外部 nav 事件直达，如 brokers → 连接管理）
*/
function _renderComp(comp) {
  // comp 是函数（工厂或箭头函数组件）→ 调用它；是元素 → 直接渲染（向后兼容）
  return typeof comp === "function" && !comp.prototype?.isReactComponent
    ? comp() : comp;
}

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
      <div className="hub-body">{_renderComp(cur.comp)}</div>
    </div>
  );
}
