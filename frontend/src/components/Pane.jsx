// Pane 容器（v3 重写）：某工作区标签页(tab)内部的分屏布局树渲染。
//
// 旧版缺陷（已根治）：
//   · 自带 panes.v2 localStorage 持久化 → 与 workbench.v1 三层交叉写，刷新竞态导致空白。
//     本版布局树完全来自 workspace store（tab.layout），组件自身零持久化。
//   · 叶子只有 pageKey 没有 params → 全站靠 sessionStorage 传参，多实例必然串台。
//     本版叶子 = { id, pageKey, params }，params 经 props 直达页面组件。
//   · 拆分固定插入 dashboard → 本版拆分复制当前叶子（同票同参，TDX 对比看盘习惯）。
//
// 所有变更（拆分/关闭/换参/激活）一律 dispatch 到 store，由 workspaceReducer 纯函数处理。
import { Suspense } from "react";
import ErrorBoundary from "./ErrorBoundary.jsx";
import { PAGES } from "../pagesRegistry.jsx";
import { leafTitle } from "../store/workspace.jsx";

export default function Pane({ tab, rootKey, seed, dispatch }) {
  return (
    <div className="pane-root">
      <PaneNode
        node={tab.layout}
        tabId={tab.id}
        activeLeafId={tab.activeLeaf}
        dispatch={dispatch}
        seed={seed}
      />
    </div>
  );
}

function PaneNode({ node, tabId, activeLeafId, dispatch, seed }) {
  if (node && node.pageKey) {
    return (
      <LeafPane
        node={node}
        tabId={tabId}
        active={activeLeafId === node.id}
        dispatch={dispatch}
        seed={seed}
      />
    );
  }
  const dir = (node && node.dir) || "h";
  return (
    <div className={`pane-split pane-split-${dir}`}>
      {((node && node.kids) || []).map((k) => (
        <PaneNode
          key={k.id}
          node={k}
          tabId={tabId}
          activeLeafId={activeLeafId}
          dispatch={dispatch}
          seed={seed}
        />
      ))}
    </div>
  );
}

function LeafPane({ node, tabId, active, dispatch }) {
  const page = PAGES[node.pageKey];
  const Comp = page?.comp;
  const label = page?.label || node.pageKey;
  const params = node.params || {};

  const onActivate = () => {
    if (!active) dispatch({ type: "LEAF_ACTIVE", tabId, leafId: node.id });
  };

  // 布局分级：终端型页面（fullBleed：行情/报价牌/市场结构/选股）满幅贴边渲染；
  // 文档型页面（表单/表格/卡片）由 .pane-leaf-body.padded 统一提供四周留白，
  // 根治「所有内容贴左边缘、界面左侧拥挤」的历史观感问题。
  const bodyCls = page?.fullBleed ? "pane-leaf-body" : "pane-leaf-body padded";

  return (
    <div
      className={`pane-leaf ${active ? "active" : ""}`}
      onMouseDown={onActivate}
    >
      <div className="pane-leaf-bar">
        <span className="pane-leaf-title">{leafTitle(node)}</span>
        <span className="pane-leaf-tools">
          <button title="左右拆分（复制当前页）"
            onClick={(e) => { e.stopPropagation(); dispatch({ type: "SPLIT_LEAF", tabId, leafId: node.id, dir: "h" }); }}>▤</button>
          <button title="上下拆分（复制当前页）"
            onClick={(e) => { e.stopPropagation(); dispatch({ type: "SPLIT_LEAF", tabId, leafId: node.id, dir: "v" }); }}>▥</button>
          <button title="关闭此分屏"
            onClick={(e) => { e.stopPropagation(); dispatch({ type: "CLOSE_LEAF", tabId, leafId: node.id }); }}>×</button>
        </span>
      </div>
      <div className={bodyCls} onMouseDown={(e) => e.stopPropagation()}>
        <ErrorBoundary>
          <Suspense key={node.id} fallback={<div className="page-loading">加载 {label}…</div>}>
            {Comp
              ? <Comp params={params} leafId={node.id} tabId={tabId} dispatch={dispatch} />
              : <div className="page-loading">页面不存在：{node.pageKey}</div>}
          </Suspense>
        </ErrorBoundary>
      </div>
    </div>
  );
}
