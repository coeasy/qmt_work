// Pane 容器：支持左右 / 上下拆分。
// 每个 Pane 持有一个 pageKey；右上角提供「左右拆 / 上下拆 / 关闭」按钮。
// 拆分比例持久化到 localStorage（按 rootKey+path 索引）。
// 体积：纯 React，不引入新依赖。
import { useEffect, useMemo, useState } from "react";
import ErrorBoundary from "./ErrorBoundary.jsx";
import { PAGES } from "../pagesRegistry.jsx";

const LS_KEY = "qmt_work.panes.v1";
// 拆分方向："h" = 左右（默认，子元素左右并排）；"v" = 上下
let _id = 0;
const nextId = () => `p${++_id}`;

function loadRoots() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length) return arr;
    }
  } catch { /* noop */ }
  return null;
}
function saveRoots(roots) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(roots)); } catch { /* noop */ }
}

// 把 Pane 树序列化为 OpenStock 持久结构：[{ id, dir?, size?, kids? }, ...]
// 叶子是 { id, pageKey }
export default function Pane({ rootKey, pageKey: initialPage, onClose }) {
  const persisted = useMemo(() => loadRoots(), []);
  // 初始：使用持久化布局的第一个根；若未持久化或键不匹配则使用传入的 pageKey
  const [roots, setRoots] = useState(() => {
    if (persisted && persisted[0]?.id === rootKey) return persisted;
    return [{ id: rootKey, kids: [{ id: nextId(), pageKey: initialPage || "dashboard" }] }];
  });
  // 当前激活的 pane id
  const [active, setActive] = useState(roots[0]?.kids?.[0]?.id || null);

  useEffect(() => { saveRoots(roots); }, [roots]);

  // 监听 nav 事件：把目标页加为新 pane（聚焦到它）
  useEffect(() => {
    const onNav = (e) => {
      const key = e.detail;
      if (!key || !PAGES[key]) return;
      setRoots((rs) => {
        const r = rs[0];
        if (!r) return rs;
        // 若已存在该 pageKey 的叶子，不重复创建
        const exists = findLeaf(r, (l) => l.pageKey === key);
        if (exists) { setActive(exists.id); return rs; }
        const newLeaf = { id: nextId(), pageKey: key };
        setActive(newLeaf.id);
        return [{ ...r, kids: [...(r.kids || []), newLeaf] }];
      });
    };
    window.addEventListener("nav", onNav);
    return () => window.removeEventListener("nav", onNav);
  }, []);

  function findLeaf(node, pred) {
    if (node.pageKey) return pred(node) ? node : null;
    for (const k of (node.kids || [])) {
      const r = findLeaf(k, pred);
      if (r) return r;
    }
    return null;
  }

  function findAndUpdate(node, id, updater) {
    if (node.id === id) return updater(node);
    if (node.kids) {
      const kids = node.kids.map((k) => findAndUpdate(k, id, updater));
      return { ...node, kids };
    }
    return node;
  }

  function findParent(node, id, parent) {
    if (node.id === id) return parent || null;
    if (node.kids) {
      for (const k of node.kids) {
        const r = findParent(k, id, node);
        if (r) return r;
      }
    }
    return null;
  }

  function splitNode(targetId, dir) {
    setRoots((rs) => {
      const r = rs[0];
      const newLeafId = nextId();
      // 在目标 leaf 同层插入一个兄弟
      const parent = findParent(r, targetId, null);
      if (parent) {
        // parent 存在：把 target 提升为 dir 容器，原 target 与 newLeaf 作为其 kids
        const newDir = { id: nextId(), dir, kids: [] };
        const upd = (node) => {
          if (node.id !== targetId) return node;
          // 把目标变成 dir
          return newDir;
        };
        const updated = findAndUpdate(r, targetId, upd);
        // 找到新 dir 节点，把 target 移动进去
        const re = (node) => {
          if (node.id === newDir.id) {
            return { ...node, kids: [node, { id: newLeafId, pageKey: "dashboard" }] };
          }
          if (node.kids) return { ...node, kids: node.kids.map(re) };
          return node;
        };
        const fixed = re(updated);
        return [{ ...rs[0], ...fixed }];
      }
      // 顶层 leaf：把 root 提升为 dir
      const upd = (node) => {
        if (node.id !== targetId) return node;
        return { id: nextId(), dir, kids: [node, { id: newLeafId, pageKey: "dashboard" }] };
      };
      return [{ ...rs[0], ...findAndUpdate(r, targetId, upd) }];
    });
  }

  function closePane(id) {
    setRoots((rs) => {
      const r = rs[0];
      const parent = findParent(r, id, null);
      if (!parent) {
        // 顶层 leaf 不能关：阻止最后关闭
        return rs;
      }
      // 把 id 的兄弟提升为 parent
      const sibling = parent.kids.find((k) => k.id !== id);
      if (!sibling) return rs;
      const grand = findParent(r, parent.id, null);
      if (grand) {
        const upd = (node) => {
          if (node.id === parent.id) return sibling;
          if (node.kids) return { ...node, kids: node.kids.map(upd) };
          return node;
        };
        return [{ ...rs[0], ...upd(r) }];
      }
      // parent 是 root：直接替换 root 的 kids 为 sibling
      return [{ ...rs[0], ...sibling, id: rs[0].id }];
    });
  }

  if (!roots[0]) return null;

  return (
    <div className="pane-root">
      <PaneNode
        node={roots[0]}
        activeId={active}
        onActive={setActive}
        onSplit={splitNode}
        onClosePane={closePane}
      />
    </div>
  );
}

function PaneNode({ node, activeId, onActive, onSplit, onClosePane }) {
  if (node.pageKey) {
    return (
      <LeafPane
        node={node}
        active={activeId === node.id}
        onActive={() => onActive(node.id)}
        onSplit={(dir) => onSplit(node.id, dir)}
        onClose={() => onClosePane(node.id)}
      />
    );
  }
  const dir = node.dir || "h";
  const cls = `pane-split pane-split-${dir}`;
  return (
    <div className={cls}>
      {(node.kids || []).map((k) => (
        <PaneNode
          key={k.id}
          node={k}
          activeId={activeId}
          onActive={onActive}
          onSplit={onSplit}
          onClosePane={onClosePane}
        />
      ))}
    </div>
  );
}

function LeafPane({ node, active, onActive, onSplit, onClose }) {
  const Comp = PAGES[node.pageKey]?.comp;
  const label = PAGES[node.pageKey]?.label || node.pageKey;
  return (
    <div className={`pane-leaf ${active ? "active" : ""}`} onMouseDown={onActive} onClick={onActive}>
      <div className="pane-leaf-bar">
        <span className="pane-leaf-title">{label}</span>
        <span className="pane-leaf-tools">
          <button title="左右拆分" onClick={() => onSplit("h")}>▤</button>
          <button title="上下拆分" onClick={() => onSplit("v")}>▥</button>
          <button title="关闭 Pane" onClick={onClose}>×</button>
        </span>
      </div>
      <div className="pane-leaf-body">
        <ErrorBoundary key={node.pageKey}>
          {Comp ? <Comp /> : <div className="page-loading">页面不存在：{node.pageKey}</div>}
        </ErrorBoundary>
      </div>
    </div>
  );
}
