import { useState } from "react";

/* 通用 Hub 容器：把一个领域下的多个页面合并到同一个顶层入口，
   内部用子页签切换。未激活的子组件会被卸载（停止其轮询/定时器）。
   - tabs: [{ key, label, comp }]，comp 为：
     a) React.lazy() 组件（typeof === "object"，有 $$typeof）
     b) 函数组件箭头函数（typeof === "function"）
     c) 已创建的 JSX 元素（向后兼容）

   渲染时每次都创建新 React 元素 <Comp key=...>，杜绝 React #31（Suspense+lazy
   复用同一元素引用）导致的渲染崩溃；同时 key 保证切 tab 时卸载旧实例。

   关键修复：React.lazy() 返回的是 object（非 function），必须通过 <Comp />
   JSX 语法触发懒加载。直接渲染 lazy 对象本身会产生空输出（不报错也不渲染）。 */

/** 判断 comp 是否需要用 <Comp /> 语法渲染（函数组件或 lazy 组件） */
function _isComponentType(comp) {
  if (!comp) return false;
  if (typeof comp === "function") return true;
  // React.lazy() 返回的对象带有 $$typeof 符号（Symbol.for('react.lazy')）
  return !!comp.$$typeof;
}

export default function Hub({ tabs, initial }) {
  const [active, setActive] = useState(initial || (tabs[0] && tabs[0].key));
  const cur = tabs.find((t) => t.key === active) || tabs[0];
  if (!cur) return null;
  const Comp = cur.comp;
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
        {_isComponentType(Comp) ? <Comp key={cur.key} /> : Comp}
      </div>
    </div>
  );
}
