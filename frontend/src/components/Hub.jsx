import { useState } from "react";

/* 通用 Hub 容器：把一个领域下的多个页面合并到同一个顶层入口，
   内部用子页签切换。未激活的子组件会被卸载（停止其轮询/定时器）。
   - tabs: [{ key, label, comp }]，comp 为组件引用（懒加载函数 / 普通组件）。
     渲染时每次都创建新 React 元素 <Comp key=...>，杜绝 React #31（Suspense+lazy
     复用同一元素引用）导致的渲染崩溃；同时 key 保证切 tab 时卸载旧实例。

   历史踩坑：旧实现用 `typeof comp === "function" ? comp() : comp` 把 lazy
   组件当函数调用 —— 但 lazy 组件不是普通函数，直接调用既不渲染也不抛错，
   结果所有 Hub 子页内容空白。本版本改为 createElement（等价于 <Comp />）。 */
function _renderTab(comp) {
  if (!comp) return null;
  if (typeof comp === "function") {
    // 函数组件 / lazy 组件：用 createElement 创建新元素（每次 render 全新实例）
    return comp;
  }
  return comp; // 已是 JSX 元素（向后兼容）
}

export default function Hub({ tabs, initial }) {
  const [active, setActive] = useState(initial || (tabs[0] && tabs[0].key));
  const cur = tabs.find((t) => t.key === active) || tabs[0];
  if (!cur) return null;
  const Comp = _renderTab(cur.comp);
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
      <div className="hub-body">
        {typeof Comp === "function" ? <Comp key={cur.key} /> : Comp}
      </div>
    </div>
  );
}
