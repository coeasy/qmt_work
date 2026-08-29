// 左侧功能树（通达信式）：分组可展开/折叠，叶子点击打开工作区标签。
import { useEffect, useState } from "react";
import { PAGE_TREE } from "../pagesRegistry.jsx";

export default function FunctionTree({ open = true, onClose }) {
  const [collapsed, setCollapsed] = useState({});
  const [active, setActive] = useState("dashboard");

  // 跟随工作区当前标签高亮
  useEffect(() => {
    const onNav = (e) => { if (e.detail) setActive(e.detail); };
    window.addEventListener("nav", onNav);
    return () => window.removeEventListener("nav", onNav);
  }, []);

  const go = (key) => window.dispatchEvent(new CustomEvent("nav", { detail: key }));
  const toggle = (g) => setCollapsed((c) => ({ ...c, [g]: !c[g] }));

  return (
    <aside className={`fn-tree ${open ? "" : "collapsed"}`}>
      <div className="fn-tree-head">
        <span className="fn-tree-title">功能</span>
        {onClose && (
          <button
            className="fn-tree-close"
            title="收起功能树（Ctrl/Cmd+B）"
            onClick={onClose}
          >‹</button>
        )}
      </div>
      <div className="fn-tree-scroll">
        {PAGE_TREE.map((grp) => (
          <div className={`fn-group ${collapsed[grp.group] ? "collapsed" : ""}`} key={grp.group}>
            <div className="fn-group-title" onClick={() => toggle(grp.group)}>
              <span className="caret">{collapsed[grp.group] ? "▸" : "▾"}</span>
              <span className="g-name">{grp.group}</span>
            </div>
            {!collapsed[grp.group] && (
              <div className="fn-items">
                {grp.items.map((it) => (
                  <div
                    key={it.key}
                    className={`fn-item ${active === it.key ? "active" : ""}`}
                    onClick={() => go(it.key)}
                  >
                    {it.label}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </aside>
  );
}
