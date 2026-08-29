// 中央工作区（v3 重写）：顶部实例级 TabBar + keep-alive 多路切换容器。
//
// 相对旧版的核心变化（旧版的三个结构性缺陷）：
//   1. 旧版 tab 存 pageKey（类型级），同一页面只能开一个 → 本版 tab 是「工作区实例」，
//      树叶子携带 pageKey + params，行情页可开 N 个实例各看各的票。
//   2. 旧版切 tab = <Pane key={rootId}> 整树卸载重挂 → 本版 alive tab 常驻 DOM
//      （非激活仅 display:none），组件状态 / WS 引用 / ECharts 实例 / 滚动位置全保留。
//   3. 旧版三层 localStorage（workbench.v1 + .roots + panes.v2）→ 本版收敛为单一
//      workspace.v3（store/workspace.jsx），schema 校验失败即回默认。
//
// 导航：仅本组件监听 window "nav"，用 normalizeNavDetail 归一化后 dispatch 到 store，
// 旧字符串 detail 调用点（FunctionTree / MenuBar / Dashboard 等）零改动继续工作。
import { Suspense, useEffect, useMemo, useState } from "react";
import Pane from "./Pane.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import {
  useWorkspace, tabTitle, activeLeaves, PRESETS,
  getAutoSplit, setAutoSplit, MAX_TABS,
} from "../store/workspace.jsx";
import { normalizeNavDetail, OPEN_IN, navTo } from "../lib/nav.js";
import { useQuotes } from "../lib/quoteHub.jsx";

export default function Workbench() {
  const { state, dispatch, activeTab, aliveIds } = useWorkspace();
  const [menuTab, setMenuTab] = useState(null);   // {id, x, y}
  const [layoutMenu, setLayoutMenu] = useState(false);
  const [autoSplit, setAutoSplitState] = useState(getAutoSplit());
  const [dragId, setDragId] = useState(null);

  // nav 事件 → store（全站唯一监听点）
  useEffect(() => {
    const onNav = (e) => {
      const norm = normalizeNavDetail(e.detail);
      if (!norm) return;
      dispatch({ type: "OPEN", pageKey: norm.pageKey, params: norm.params, openIn: norm.openIn });
    };
    window.addEventListener("nav", onNav);
    return () => window.removeEventListener("nav", onNav);
  }, [dispatch]);

  // 点击任意处关闭弹层菜单
  useEffect(() => {
    if (!menuTab && !layoutMenu) return;
    const close = () => { setMenuTab(null); setLayoutMenu(false); };
    document.addEventListener("click", close);
    document.addEventListener("contextmenu", close, true);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("contextmenu", close, true);
    };
  }, [menuTab, layoutMenu]);

  const toggleAutoSplit = () => {
    const next = !autoSplit;
    setAutoSplit(next);
    setAutoSplitState(next);
  };

  const activeId = activeTab ? activeTab.id : null;
  const aliveTabs = state.tabs.filter((t) => aliveIds.has(t.id));

  return (
    <div className="workbench">
      <div className="wb-tabs" onWheel={onTabWheel}>
        {state.tabs.map((t, idx) => (
          <TabItem
            key={t.id}
            tab={t}
            index={idx}
            total={state.tabs.length}
            active={t.id === activeId}
            dimmed={!aliveIds.has(t.id)}
            dragId={dragId}
            setDragId={setDragId}
            onClick={() => dispatch({ type: "ACTIVATE", tabId: t.id })}
            onMiddleClose={() => dispatch({ type: "CLOSE_TAB", tabId: t.id })}
            onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); setMenuTab({ id: t.id, x: e.clientX, y: e.clientY }); setLayoutMenu(false); }}
            onMove={(from, to) => dispatch({ type: "MOVE_TAB", from, to })}
          />
        ))}
        <div className="wb-tabs-tools">
          <button
            className="wb-tools-btn"
            title={state.tabs.length >= MAX_TABS ? `已达上限 ${MAX_TABS} 个` : "新建窗口（复制当前页为新实例）"}
            disabled={state.tabs.length >= MAX_TABS || !activeTab}
            onClick={(e) => {
              e.stopPropagation();
              if (!activeTab) return;
              const fl = activeLeaves(activeTab).find((l) => l.id === activeTab.activeLeaf) || activeLeaves(activeTab)[0];
              if (fl) navTo(fl.pageKey, { params: fl.params, openIn: OPEN_IN.TAB });
            }}
          >＋</button>
          <div className="wb-layout-wrap">
            <button className="wb-tools-btn" title="分屏布局预设"
              onClick={(e) => { e.stopPropagation(); setLayoutMenu((v) => !v); setMenuTab(null); }}>☰ 布局</button>
            {layoutMenu && (
              <div className="wb-menu wb-menu-layout" onClick={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
                {Object.entries(PRESETS).map(([k, v]) => (
                  <div key={k} className="wb-menu-item"
                    onClick={() => { dispatch({ type: "APPLY_PRESET", tabId: activeId, preset: k }); setLayoutMenu(false); }}>
                    {v.label}
                  </div>
                ))}
                <div className="wb-menu-sep" />
                <div className="wb-menu-item"
                  onClick={() => { dispatch({ type: "RESET_LAYOUT", tabId: activeId }); setLayoutMenu(false); }}>
                  ⟲ 重置当前分屏
                </div>
                <div className="wb-menu-sep" />
                <div className={`wb-menu-item wb-menu-toggle ${autoSplit ? "on" : ""}`}
                  onClick={toggleAutoSplit}>
                  <span className="wb-switch"><i /></span>
                  自动分屏（新页面自动填入当前窗）
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {menuTab && (
        <div className="wb-menu" style={{ left: menuTab.x, top: menuTab.y }}
          onClick={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
          <MenuItem onClick={() => { dispatch({ type: "ACTIVATE", tabId: menuTab.id }); setMenuTab(null); }}>
            切换到此窗口
          </MenuItem>
          <MenuItem onClick={() => { dispatch({ type: "SPLIT", tabId: menuTab.id, dir: "h" }); setMenuTab(null); }}>
            ▤ 左右拆分（复制当前页）
          </MenuItem>
          <MenuItem onClick={() => { dispatch({ type: "SPLIT", tabId: menuTab.id, dir: "v" }); setMenuTab(null); }}>
            ▥ 上下拆分（复制当前页）
          </MenuItem>
          <div className="wb-menu-sep" />
          <MenuItem onClick={() => { dispatch({ type: "CLOSE_TAB", tabId: menuTab.id }); setMenuTab(null); }}>
            关闭窗口
          </MenuItem>
          <MenuItem onClick={() => { dispatch({ type: "CLOSE_OTHERS", tabId: menuTab.id }); setMenuTab(null); }}>
            关闭其他窗口
          </MenuItem>
          <MenuItem onClick={() => { dispatch({ type: "CLOSE_RIGHT", tabId: menuTab.id }); setMenuTab(null); }}>
            关闭右侧窗口
          </MenuItem>
          <MenuItem onClick={() => { dispatch({ type: "PIN_TAB", tabId: menuTab.id }); setMenuTab(null); }}>
            {(() => {
              const t = state.tabs.find((x) => x.id === menuTab.id);
              return t && t.pinned ? "取消固定" : "固定（不受关闭其他影响）";
            })()}
          </MenuItem>
        </div>
      )}

      <div className="wb-body">
        {aliveTabs.length === 0 && <div className="page-loading">加载中…</div>}
        {/* keep-alive 核心：所有 alive tab 的 Pane 常驻挂载，非激活仅 visibility:hidden
            （保持布局尺寸，ECharts 不归零）。组件内部状态（ECharts 实例 / 输入框 /
            滚动位置 / 订阅）在切换间零销毁。LRU 超限的 tab 不在此列 —— 激活时重新
            挂载，并从 store 恢复参数与布局。 */}
        {aliveTabs.map((t) => (
          <div key={t.id} className={`wb-tabpane${t.id === activeId ? "" : " inactive"}`}
            aria-hidden={t.id !== activeId}>
            <ErrorBoundary key={t.id}>
              <Pane tab={t} rootKey={t.id} seed={t.layout} dispatch={dispatch} />
            </ErrorBoundary>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ======================== Tab 项（含迷你行情） ======================== */
function TabItem({ tab, active, dimmed, dragId, setDragId, onClick, onMiddleClose, onContextMenu, onMove }) {
  const leaves = useMemo(() => activeLeaves(tab), [tab]);
  const codes = useMemo(() => leaves.map((l) => l.params && l.params.code).filter(Boolean), [leaves]);
  const { quotes } = useQuotes(codes);

  const cls = ["wb-tab"];
  if (active) cls.push("active");
  if (dimmed) cls.push("dimmed");
  if (dragId === tab.id) cls.push("dragging");

  return (
    <div
      className={cls.join(" ")}
      title={`${tabTitle(tab)}（左键切换 / 右键菜单 / 中键关闭 / 拖动排序）`}
      draggable
      onDragStart={(e) => { setDragId(tab.id); e.dataTransfer.effectAllowed = "move"; }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        if (dragId && dragId !== tab.id) onMove(dragId, tab.id);
        setDragId(null);
      }}
      onDragEnd={() => setDragId(null)}
      onClick={onClick}
      onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); onMiddleClose(); } }}
      onContextMenu={onContextMenu}
    >
      <span className="wb-tab-label">
        {tab.pinned ? "📌 " : ""}{tabTitle(tab)}
      </span>
      {codes.map((c, i) => {
        const q = quotes[c];
        const last = q && q.last != null ? Number(q.last) : null;
        if (last == null) return null;
        const pre = q.pre_close != null ? Number(q.pre_close) : (q.close != null ? Number(q.close) : null);
        const pct = pre ? ((last - pre) / pre) * 100 : null;
        const cls2 = ["wb-tab-mini"];
        if (pct != null) cls2.push(pct >= 0 ? "up" : "down");
        return (
          <span key={i} className={cls2.join(" ")} title={`${q.name || ""} ${last.toFixed(2)}`}>
            {last.toFixed(2)}{pct != null ? ` ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%` : ""}
          </span>
        );
      })}
      <span className="wb-tab-close" title="关闭"
        onClick={(e) => { e.stopPropagation(); onMiddleClose(); }}>×</span>
    </div>
  );
}

/* ======================== 右键菜单项 ======================== */
function MenuItem({ children, disabled, onClick }) {
  return (
    <div className={`wb-menu-item${disabled ? " disabled" : ""}`}
      onClick={disabled ? undefined : onClick}>
      {children}
    </div>
  );
}

/* ======================== 滚动支持 ======================== */
function onTabWheel(e) {
  // 横向溢出时，滚轮转成横向滚动（多 tab 场景常见）
  if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
  if (e.currentTarget.scrollWidth <= e.currentTarget.clientWidth) return;
  e.currentTarget.scrollLeft += e.deltaY;
  e.preventDefault();
}

