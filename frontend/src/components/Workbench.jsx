// 中央工作区（通达信式 MDI）：多标签文档 + Pane 拆分/分屏。
// 顶部 tab 列表（每个 tab 对应一个 Pane root）；body 用 Pane.jsx 渲染可拆分的树。
// 监听 window "nav" 事件：加新页到当前 root；布局持久化到 localStorage。
import { useEffect, useState } from "react";
import Pane from "./Pane.jsx";
import { PAGES, DEFAULT_PAGE } from "../pagesRegistry.jsx";

const LS_KEY = "qmt_work.workbench.v1";

function loadState() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      const o = JSON.parse(raw);
      if (Array.isArray(o.openTabs) && o.openTabs.length) {
        const tabs = o.openTabs.filter((k) => PAGES[k]);
        const active = tabs.includes(o.active) ? o.active : tabs[0];
        return { tabs, active };
      }
    }
  } catch { /* noop */ }
  return { tabs: [DEFAULT_PAGE], active: DEFAULT_PAGE };
}

let _wbid = 0;
const nextId = () => `wb${++_wbid}`;

export default function Workbench() {
  const [state, setState] = useState(loadState);
  const { tabs, active } = state;
  // tab -> rootId；持久化：tabKey 序列化为对象 {tab: id}
  const [rootMap, setRootMap] = useState(() => {
    try {
      const raw = localStorage.getItem(LS_KEY + ".roots");
      if (raw) return JSON.parse(raw);
    } catch { /* noop */ }
    return null;
  });

  useEffect(() => {
    try { localStorage.setItem(LS_KEY, JSON.stringify({ openTabs: tabs, active })); } catch { /* noop */ }
  }, [tabs, active]);

  useEffect(() => {
    if (rootMap) {
      try { localStorage.setItem(LS_KEY + ".roots", JSON.stringify(rootMap)); } catch { /* noop */ }
    }
  }, [rootMap]);

  // 确保每个 tab 都有 rootId
  function getRootId(tab) {
    if (rootMap && rootMap[tab]) return rootMap[tab];
    return null;
  }

  function ensureRoot(tab) {
    if (rootMap && rootMap[tab]) return rootMap[tab];
    const id = nextId();
    setRootMap((m) => ({ ...(m || {}), [tab]: id }));
    return id;
  }

  useEffect(() => {
    // 首次为所有 tabs 预生成 root
    setRootMap((m) => {
      const out = { ...(m || {}) };
      tabs.forEach((t) => { if (!out[t]) out[t] = nextId(); });
      return out;
    });
  }, [tabs]);

  // 监听 nav：tab 不存在则新增，激活
  useEffect(() => {
    const onNav = (e) => {
      const key = e.detail;
      if (!key || !PAGES[key]) return;
      setState((s) => {
        const has = s.tabs.includes(key);
        const nt = has ? s.tabs : [...s.tabs, key];
        if (!has) ensureRoot(key);
        return { tabs: nt, active: key };
      });
    };
    window.addEventListener("nav", onNav);
    return () => window.removeEventListener("nav", onNav);
  }, []);

  function closeTab(key) {
    setState((s) => {
      const nt = s.tabs.filter((k) => k !== key);
      let na = s.active;
      if (na === key) na = nt[nt.length - 1] || DEFAULT_PAGE;
      setRootMap((m) => {
        const o = { ...(m || {}) };
        delete o[key];
        return o;
      });
      return { tabs: nt.length ? nt : [DEFAULT_PAGE], active: na };
    });
  }

  const activeLabel = PAGES[active]?.label || active;
  const activeRootId = getRootId(active);
  const activeSeed = active;

  return (
    <div className="workbench">
      <div className="wb-tabs">
        {tabs.map((k) => (
          <div
            key={k}
            className={`wb-tab ${k === active ? "active" : ""}`}
            onClick={() => setState((s) => ({ ...s, active: k }))}
            title={PAGES[k]?.label}
          >
            <span className="wb-tab-label">{PAGES[k]?.label || k}</span>
            <span
              className="wb-tab-close"
              onClick={(e) => { e.stopPropagation(); closeTab(k); }}
            >×</span>
          </div>
        ))}
      </div>
      <div className="wb-body">
        {activeRootId ? (
          <Pane rootKey={activeRootId} pageKey={activeSeed} />
        ) : (
          <div className="page-loading">加载中…</div>
        )}
      </div>
    </div>
  );
}
