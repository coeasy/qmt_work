// WorkspaceStore v3 —— 前端工作区的单一状态源。
//
// 设计要点（根治历史空白/错位问题的关键）：
// 1. Tab = 「工作区实例」，内含一棵分屏 layout 树；树叶子才是「页面实例」(pageKey + params)。
//    → 行情页可开 N 个实例各看各的票，其它页面默认单例。
// 2. 所有树结构变更都是纯函数（见下方 *Tree* 系列），可在测试中直接断言。
// 3. 单一 localStorage key + schema 校验：校验失败立即回默认，永不把损坏数据渲染成空白。
// 4. 旧 v1/workbench + v2/panes 一次性迁移，迁移本身也在 try/catch 内。
// 5. aliveOrder 控制 keep-alive 常驻上限（LRU）：超出的 tab 停止渲染 DOM，
//    激活时重新挂载并从本 store 恢复全部参数与布局。
import { createContext, useContext, useEffect, useMemo, useReducer, useRef } from "react";
import { PAGES, DEFAULT_PAGE, resolveKey, KEY_ALIAS } from "../pagesRegistry.jsx";

const LS_KEY = "qmt_work.workspace.v3";
const OLD_WB_KEY = "qmt_work.workbench.v1";
const OLD_PANES_KEY = "qmt_work.panes.v2";
const LS_AUTO_SPLIT = "qmt_work.autoSplit.v1";

export const MAX_ALIVE = 8;      // keep-alive 常驻上限（可被 Settings 覆盖）
export const MAX_TABS = 24;      // 硬上限，防止误操作堆满

// 允许开多个实例的页面（行情类）。其余页面为「单例」：已存在同 pageKey 的 tab 时直接激活。
export const MULTI_INSTANCE_PAGES = new Set(["quote"]);

/* ======================== id 生成 ======================== */
let _seq = 0;
// 时间戳(36进制) + 单调序号：跨会话永不复用，避免与持久化遗留 id 碰撞。
const newId = (p) => `${p}${Date.now().toString(36)}${(_seq++).toString(36)}x`;

/* ======================== 叶子 / 树（纯函数） ======================== */
// 把旧 key 携带的子页签参数并入 params（阶段一：旧 key → 中心页，tab 定位子页签）。
function aliasParams(key, params) {
  const target = KEY_ALIAS[key];
  if (target) return { ...params, tab: (params && params.tab) || key };
  return params;
}

export function makeLeaf(pageKey, params = {}) {
  const ok = resolveKey(pageKey);
  return { id: newId("l_"), pageKey: ok, params: { ...aliasParams(pageKey, params) } };
}

export function makeContainer(dir, kids) {
  return { id: newId("g_"), dir: dir || "h", kids };
}

export function defaultLayoutFor(pageKey, params) {
  return { id: newId("g_"), dir: "h", kids: [makeLeaf(pageKey, params)] };
}

export function findNode(node, id) {
  if (!node || typeof node !== "object") return null;
  if (node.id === id) return node;
  for (const k of node.kids || []) {
    const z = findNode(k, id);
    if (z) return z;
  }
  return null;
}

export function findParent(node, id) {
  if (!node || !node.kids) return null;
  const idx = node.kids.findIndex((k) => k && k.id === id);
  if (idx >= 0) return { parent: node, idx };
  for (const k of node.kids) {
    const r = findParent(k, id);
    if (r) return r;
  }
  return null;
}

export function collectLeaves(node, out = []) {
  if (!node || typeof node !== "object") return out;
  if (node.pageKey) { out.push(node); return out; }
  (node.kids || []).forEach((k) => collectLeaves(k, out));
  return out;
}

export function firstLeaf(node) {
  if (node && node.pageKey) return node;
  for (const k of (node && node.kids) || []) {
    const r = firstLeaf(k);
    if (r) return r;
  }
  return null;
}

export function replaceKid(node, parentId, newNode) {
  if (!node) return node;
  if (node.id === parentId) return newNode;
  if (!node.kids) return node;
  return { ...node, kids: node.kids.map((k) => replaceKid(k, parentId, newNode)) };
}

// 净化布局树：剔除非法 pageKey、提级单子容器、丢弃空容器、保证至少 1 个叶子。
// pageKey 合法性以页面注册表 PAGES 为准（历史上此处曾误写为与 seed 比较，
// 导致刷新后所有叶子被改写成 seed 页面但 params 残留 —— 「仪表盘 · 399001.SZ」腐蚀的根源）。
export function sanitizeTree(node, seed) {
  const fallback = seed && seed.pageKey ? seed : { pageKey: DEFAULT_PAGE, params: {} };
  if (!node || typeof node !== "object") return makeLeaf(fallback.pageKey, fallback.params);
  if (node.pageKey) return makeLeaf(node.pageKey, node.params);
  const kids = (node.kids || []).map((k) => sanitizeTree(k, fallback)).filter(Boolean);
  if (kids.length === 0) return makeLeaf(fallback.pageKey, fallback.params);
  if (kids.length === 1) return kids[0];
  return { id: node.id || newId("g_"), dir: node.dir || "h", kids };
}

// 拆分：把 target 叶子包装成 dir 容器，插入一个新兄弟叶子。
export function splitTree(node, targetId, dir, newLeaf) {
  const target = findNode(node, targetId);
  if (!target) return node;
  const holder = { id: newId("g_"), dir: dir || "h", kids: [target, newLeaf] };
  const upd = (n) => (n.id === targetId ? holder : n.kids ? { ...n, kids: n.kids.map(upd) } : n);
  return upd(node);
}

// 关闭叶子：兄弟提级到父位；父若为根则沿用根 id。
export function closeTree(node, id) {
  if (!node) return node;
  if (collectLeaves(node).length <= 1) return node;   // 唯一叶子：拒绝
  const loc = findParent(node, id);
  if (!loc) return node;
  const sibling = loc.parent.kids.find((k) => k.id !== id);
  if (!sibling) return node;
  if (loc.parent === node) return { ...sibling, id: node.id };
  return replaceKid(node, loc.parent.id, sibling);
}

// 更新叶子参数（换股票/换周期）。
export function setLeafParams(node, id, params) {
  const upd = (n) => {
    if (n.pageKey && n.id === id) return { ...n, params: { ...n.params, ...params } };
    if (n.kids) return { ...n, kids: n.kids.map(upd) };
    return n;
  };
  return upd(node);
}

/* ======================== 分屏预设 ======================== */
// 槽位不足的槽用「最后一个叶子克隆」补齐（同票不同指标，TDX 常用）；
// 叶子数多于槽位时，多余的按原顺序继续挂在最后一个槽右侧，绝不丢弃窗口。
export const PRESETS = {
  "1x1": { label: "1×1 单窗口" },
  "1x2": { label: "1×2 左右对比" },
  "2x1": { label: "2×1 上下对比" },
  "2x2": { label: "2×2 四宫格" },
  "1+2": { label: "1+2 主看+双辅" },
  "2+1": { label: "2+1 双上+单下" },
};

export function buildPreset(key, leaves) {
  const src = leaves.length ? leaves.slice() : [makeLeaf(DEFAULT_PAGE)];
  let i = 0;
  const take = () => (i < src.length ? src[i++] : makeLeaf(src[src.length - 1].pageKey, { ...src[src.length - 1].params }));
  const L = take;
  const H = (a, b) => makeContainer("h", [a, b]);
  const V = (a, b) => makeContainer("v", [a, b]);
  const chain = (head, rest) => rest.reduce((acc, r) => H(acc, r), head);
  switch (key) {
    case "1x2": return H(L(), L());
    case "2x1": return V(L(), L());
    case "2x2": return V(H(L(), L()), H(L(), L()));
    case "1+2": return H(L(), V(L(), L()));
    case "2+1": return V(L(), H(L(), L()));
    default: {
      if (src.length <= 1) return L();
      // 1x1 但已有多个窗口：保持现状，不丢窗口
      return null;
    }
  }
}

// 把剩余叶子按原顺序水平串接（buildPreset 内 chain 的模块级版本，语义一致：
// 多余叶子逐个挂到前一格右侧，绝不丢弃窗口）。
function chainLeaves(head, rest) {
  return rest.reduce((acc, r) => makeContainer("h", [acc, r]), head);
}

// 应用预设后把剩余叶子追加到最后一格右侧。
export function applyPreset(layout, key, leaves) {
  const built = buildPreset(key, leaves.slice(0, presetSlots(key)));
  if (!built) return layout;
  const rest = leaves.slice(presetSlots(key));
  let next = rest.length ? built : built;
  if (rest.length) {
    const extra = chainLeaves(rest[0], rest.slice(1));
    next = attachRightmost(built, extra);
  }
  return sanitizeTree(next, { pageKey: leaves[0] ? leaves[0].pageKey : DEFAULT_PAGE, params: {} });
}

function presetSlots(key) {
  return ({ "1x1": 1, "1x2": 2, "2x1": 2, "2x2": 4, "1+2": 3, "2+1": 3 })[key] || 1;
}

// 找到最深的叶子，把它包装成容器并塞入 extra。
function attachRightmost(node, extra) {
  const leaf = lastLeaf(node);
  if (!leaf) return node;
  const holder = makeContainer("h", [leaf, extra]);
  const upd = (n) => (n.id === leaf.id ? holder : n.kids ? { ...n, kids: n.kids.map(upd) } : n);
  return upd(node);
}

function lastLeaf(node) {
  if (!node) return null;
  if (node.pageKey) return node;
  const kids = node.kids || [];
  for (let i = kids.length - 1; i >= 0; i--) {
    const r = lastLeaf(kids[i]);
    if (r) return r;
  }
  return null;
}

/* ======================== 标题 ======================== */
// 行情页标题带代码；其余页面仅用注册表 label。
export function leafTitle(leaf) {
  const base = PAGES[leaf.pageKey] ? PAGES[leaf.pageKey].label : leaf.pageKey;
  const code = leaf.params && leaf.params.code;
  return code ? `${base} · ${code}` : base;
}

export function tabTitle(tab) {
  const leaves = collectLeaves(tab.layout);
  if (leaves.length === 1) return leafTitle(leaves[0]);
  return `${leafTitle(leaves[0])} · 分屏${leaves.length}窗`;
}

/* ======================== 默认状态 ======================== */
function makeDefaultTab(pageKey = DEFAULT_PAGE, params = {}) {
  const layout = defaultLayoutFor(pageKey, params);
  return { id: newId("t_"), layout, activeLeaf: layout.kids[0].id, pinned: false };
}

export function defaultState() {
  const tab = makeDefaultTab();
  return { version: 3, tabs: [tab], activeId: tab.id, aliveOrder: [tab.id] };
}

/* ======================== schema 校验 ======================== */
export function validateState(s) {
  try {
    if (!s || typeof s !== "object" || s.version !== 3) return null;
    if (!Array.isArray(s.tabs) || s.tabs.length === 0) return null;
    const tabs = [];
    for (const t of s.tabs) {
      if (!t || !t.id || typeof t.id !== "string") return null;
      if (!t.layout) return null;
      const layout = sanitizeTree(t.layout, { pageKey: DEFAULT_PAGE, params: {} });
      const leaves = collectLeaves(layout);
      const activeLeaf = leaves.some((l) => l.id === t.activeLeaf) ? t.activeLeaf : leaves[0].id;
      tabs.push({ id: t.id, layout, activeLeaf, pinned: !!t.pinned });
    }
    const activeId = tabs.some((t) => t.id === s.activeId) ? s.activeId : tabs[0].id;
    return { version: 3, tabs, activeId, aliveOrder: orderFromTabs(tabs, s.aliveOrder) };
  } catch {
    return null;
  }
}

function orderFromTabs(tabs, given) {
  const ids = tabs.map((t) => t.id);
  const arr = Array.isArray(given) ? given.filter((id) => ids.includes(id)) : [];
  ids.forEach((id) => { if (!arr.includes(id)) arr.push(id); });
  return arr.slice(0, ids.length);
}

/* ======================== localStorage 读写 + 迁移 ======================== */
function readJson(key) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const o = JSON.parse(raw);
    return o && typeof o === "object" ? o : null;
  } catch { return null; }
}

function writeJson(key, o) {
  try { localStorage.setItem(key, JSON.stringify(o)); } catch { /* quota */ }
}

function removeKey(key) {
  try { localStorage.removeItem(key); } catch { /* noop */ }
}

// 旧版迁移：workbench.v1(已开 pageKey 列表) + panes.v2(每 root 布局树) → v3 单 store。
function migrateLegacy() {
  try {
    const wb = readJson(OLD_WB_KEY);
    const panes = readJson(OLD_PANES_KEY);
    let keys = [];
    if (wb && Array.isArray(wb.openTabs) && wb.openTabs.length) {
      keys = wb.openTabs.map(resolveKey).filter((k, i, a) => k && a.indexOf(k) === i);
    }
    if (!keys.length) keys = [DEFAULT_PAGE];

    const tabs = keys.map((k) => {
      const legacy = panes && panes[`wb_${k}`];
      const layout = legacy ? sanitizeTree(legacy, { pageKey: k, params: {} }) : defaultLayoutFor(k, {});
      return { id: newId("t_"), layout, activeLeaf: firstLeaf(layout).id, pinned: false };
    });
    return { version: 3, tabs, activeId: tabs[0].id, aliveOrder: tabs.map((t) => t.id) };
  } catch {
    return null;
  }
}

function loadState() {
  const fresh = validateState(readJson(LS_KEY));
  if (fresh) return fresh;
  const migrated = migrateLegacy();
  if (migrated) {
    removeKey(OLD_WB_KEY);
    removeKey(OLD_PANES_KEY);
    return migrated;
  }
  return defaultState();
}

/* ======================== 自动分屏开关 ======================== */
export function getAutoSplit() {
  try { return localStorage.getItem(LS_AUTO_SPLIT) === "1"; } catch { return false; }
}
export function setAutoSplit(on) {
  try { localStorage.setItem(LS_AUTO_SPLIT, on ? "1" : "0"); } catch { /* noop */ }
}

/* ======================== reducer ======================== */
// LRU 语义（统一为「头部 = 最近使用」）：withAlive 把 tabId 放到 aliveOrder 头部，
// 渲染层 aliveIds 取 aliveOrder 的**头部** MAX_ALIVE 个。历史缺陷：渲染层曾用
// slice(-MAX_ALIVE) 取尾部，而激活路径把最新 tab 放头部 —— 一旦打开超过 8 个
// 标签页，刚激活（或刚关闭其它标签后聚焦）的 tab 恰好被挤出 alive 集合，
// wb-body 内不渲染它的 Pane → 「tab 存在但内容区一片空白」。两处语义必须一致。
function withAlive(state, tabId) {
  const order = [tabId, ...state.aliveOrder.filter((id) => id !== tabId)];
  return { ...state, aliveOrder: order.slice(0, Math.max(state.tabs.length, MAX_ALIVE)) };
}

function mapTab(state, tabId, fn) {
  return { ...state, tabs: state.tabs.map((t) => (t.id === tabId ? fn(t) : t)) };
}

function findTab(state, id) {
  return state.tabs.find((t) => t.id === id) || null;
}

function activate(state, tabId, opts = {}) {
  if (!findTab(state, tabId)) return state;
  // tabId 必须传入 withAlive：漏传会让新 tab 永远不进 aliveOrder（keep-alive 失效，
  // 渲染层 aliveTabs 过滤后新 tab 无 pane，表现为「tab 存在但内容区空白」）
  return { ...withAlive({ ...state, activeId: tabId }, tabId), ...(opts.pinOrder ? {} : {}) };
}

function openNewTab(state, pageKey, params, openIn) {
  if (state.tabs.length >= MAX_TABS) return state;
  const tab = makeDefaultTab(pageKey, params);
  const base = { ...state, tabs: [...state.tabs, tab] };
  if (openIn === "background") return withAlive({ ...base, activeId: state.activeId }, tab.id);
  return activate(base, tab.id);
}

export function workspaceReducer(state, action) {
  switch (action.type) {
    case "OPEN": {
      const { pageKey, params, openIn } = action;
      const key = resolveKey(pageKey);
      const cur = findTab(state, state.activeId);
      const curLeaf = cur ? findNode(cur.layout, cur.activeLeaf) : null;

      if (openIn === "replace") {
        if (!cur || !curLeaf) return state;
        return { ...state, tabs: state.tabs.map((t) => t.id === cur.id ? { ...t, layout: setLeafParams(t.layout, cur.activeLeaf, params) } : t) };
      }
      if (openIn === "split-h" || openIn === "split-v") {
        if (!cur) return openNewTab(state, key, params, "tab");
        if (collectLeaves(cur.layout).length >= 4) return openNewTab(state, key, params, "tab"); // 满屏则新开窗
        const targetId = curLeaf ? cur.activeLeaf : firstLeaf(cur.layout).id;
        const leaf = makeLeaf(key, params);
        const layout = sanitizeTree(splitTree(cur.layout, targetId, openIn === "split-v" ? "v" : "h", leaf), { pageKey: key, params });
        return withAlive(mapTab(state, cur.id, (t) => ({ ...t, layout, activeLeaf: leaf.id })));
      }
      if (openIn === "tab" || openIn === "background") {
        return openNewTab(state, key, params, openIn);
      }
      // auto：① 当前激活叶子同类 → 直接换参数（根治「点了没反应」）
      if (curLeaf && curLeaf.pageKey === key) {
        return mapTab(state, cur.id, (t) => ({ ...t, layout: setLeafParams(t.layout, cur.activeLeaf, params) }));
      }
      // ② 单例页面已有 tab → 聚焦（可带上参数）
      const same = state.tabs.find((t) => collectLeaves(t.layout).some((l) => l.pageKey === key));
      if (same && !MULTI_INSTANCE_PAGES.has(key)) {
        const sLeaf = findNode(same.layout, same.activeLeaf);
        const layout = sLeaf ? setLeafParams(same.layout, same.activeLeaf, params) : same.layout;
        // tabId 必须传：activate(state, tabId) 首行是 `if (!findTab(state, tabId)) return state`，
        // 漏传 → findTab(state, undefined) === null → **原样返回旧 state**，
        // 即「tab 已在但切不过去」：点菜单无任何反应、界面停留在旧页（2026-09-12 实测）。
        return activate(mapTab(state, same.id, (t) => ({ ...t, layout })), same.id);
      }
      // ③ 自动分屏开启且当前 tab 不是多实例页面 → 拆进当前 tab
      if (getAutoSplit() && cur && !MULTI_INSTANCE_PAGES.has(curLeaf ? curLeaf.pageKey : key) && collectLeaves(cur.layout).length < 4) {
        const leaf = makeLeaf(key, params);
        const layout = sanitizeTree(splitTree(cur.layout, cur.activeLeaf, "v", leaf), { pageKey: key, params });
        return withAlive(mapTab(state, cur.id, (t) => ({ ...t, layout, activeLeaf: leaf.id })));
      }
      // ④ 兜底：新开窗
      return openNewTab(state, key, params, "tab");
    }

    case "ACTIVATE": return activate(state, action.tabId);

    case "CLOSE_TAB": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      const pinned = !!t.pinned;
      if (!pinned && state.tabs.length <= 1) {
        // 唯一窗：重置布局而不是真的关掉（避免空白）
        return mapTab(state, t.id, (x) => {
          const layout = sanitizeTree(defaultLayoutFor(firstLeaf(x.layout).pageKey, firstLeaf(x.layout).params), { pageKey: DEFAULT_PAGE, params: {} });
          return { ...x, layout, activeLeaf: firstLeaf(layout).id };
        });
      }
      const tabs = state.tabs.filter((x) => x.id !== action.tabId);
      if (tabs.length === 0) {
        const nt = makeDefaultTab();
        return { version: 3, tabs: [nt], activeId: nt.id, aliveOrder: [nt.id] };
      }
      const activeId = state.activeId === action.tabId
        ? (tabs.find((x) => x.id !== state.activeId) || tabs[tabs.length - 1]).id
        : state.activeId;
      return { ...activate({ ...state, tabs, aliveOrder: state.aliveOrder.filter((id) => id !== action.tabId) }, activeId) };
    }

    case "CLOSE_OTHERS": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      return activate({ ...state, tabs: [t], aliveOrder: [t.id] }, t.id);
    }

    case "CLOSE_RIGHT": {
      const idx = state.tabs.findIndex((x) => x.id === action.tabId);
      if (idx < 0) return state;
      const t = state.tabs[idx];
      const tabs = state.tabs.slice(0, idx + 1);
      if (tabs.length === 0) { const nt = makeDefaultTab(); return { version: 3, tabs: [nt], activeId: nt.id, aliveOrder: [nt.id] }; }
      return activate({ ...state, tabs, aliveOrder: state.aliveOrder.filter((id) => tabs.some((x) => x.id === id)) }, t.id);
    }

    case "PIN_TAB":
      return mapTab(state, action.tabId, (t) => ({ ...t, pinned: !t.pinned }));

    case "MOVE_TAB": {
      const tabs = state.tabs.slice();
      const from = tabs.findIndex((x) => x.id === action.from);
      const to = tabs.findIndex((x) => x.id === action.to);
      if (from < 0 || to < 0) return state;
      const [moved] = tabs.splice(from, 1);
      tabs.splice(to, 0, moved);
      return { ...state, tabs };
    }

    case "SPLIT": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      if (collectLeaves(t.layout).length >= 4) return state; // 分屏上限 4 窗
      const src = findNode(t.layout, t.activeLeaf) || firstLeaf(t.layout);
      if (!src) return state;
      // 复制当前激活叶子（同票同参）—— TDX 对比看盘习惯
      const leaf = makeLeaf(src.pageKey, src.params || {});
      const layout = sanitizeTree(splitTree(t.layout, src.id, action.dir || "h", leaf), { pageKey: src.pageKey, params: src.params || {} });
      return withAlive(mapTab(state, t.id, (x) => ({ ...x, layout, activeLeaf: leaf.id })));
    }

    case "SPLIT_LEAF": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      if (collectLeaves(t.layout).length >= 4) return state; // 分屏上限 4 窗
      const src = findNode(t.layout, action.leafId);
      if (!src) return state;
      const leaf = makeLeaf(src.pageKey, src.params || {});
      const layout = sanitizeTree(splitTree(t.layout, action.leafId, action.dir || "h", leaf), { pageKey: src.pageKey, params: src.params || {} });
      return withAlive(mapTab(state, t.id, (x) => ({ ...x, layout, activeLeaf: leaf.id })));
    }

    case "CLOSE_LEAF": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      if (collectLeaves(t.layout).length <= 1) return state;
      const layout = closeTree(t.layout, action.leafId);
      const leaves = collectLeaves(layout);
      const activeLeaf = t.activeLeaf === action.leafId ? (leaves[0] ? leaves[0].id : t.activeLeaf) : t.activeLeaf;
      return mapTab(state, t.id, (x) => ({ ...x, layout, activeLeaf }));
    }

    case "LEAF_PARAMS": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      return mapTab(state, t.id, (x) => ({ ...x, layout: setLeafParams(x.layout, action.leafId, action.params) }));
    }

    case "LEAF_ACTIVE":
      return mapTab(state, action.tabId, (t) => ({ ...t, activeLeaf: action.leafId }));

    case "APPLY_PRESET": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      const leaves = collectLeaves(t.layout);
      const layout = applyPreset(t.layout, action.preset, leaves);
      const first = firstLeaf(layout);
      return mapTab(state, t.id, (x) => ({ ...x, layout, activeLeaf: first.id }));
    }

    case "RESET_LAYOUT": {
      const t = findTab(state, action.tabId);
      if (!t) return state;
      const fl = firstLeaf(t.layout);
      const layout = sanitizeTree(defaultLayoutFor(fl.pageKey, fl.params), { pageKey: DEFAULT_PAGE, params: {} });
      return mapTab(state, t.id, (x) => ({ ...x, layout, activeLeaf: firstLeaf(layout).id }));
    }

    default:
      return state;
  }
}

/* ======================== Provider ======================== */
const Ctx = createContext(null);

export function WorkspaceProvider({ children }) {
  const [state, dispatch] = useReducer(workspaceReducer, undefined, loadState);
  const timerRef = useRef(null);

  // 防抖持久化：状态稳定 200ms 后写一次，避免高频渲染打满 localStorage。
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      const { version, tabs, activeId, aliveOrder } = state;
      writeJson(LS_KEY, { version, tabs, activeId, aliveOrder });
    }, 200);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [state]);

  const value = useMemo(() => ({
    state,
    dispatch,
    activeTab: state.tabs.find((t) => t.id === state.activeId) || state.tabs[0],
    // 与 withAlive 同语义：取头部（最近使用）MAX_ALIVE 个，激活 tab 永远在列
    aliveIds: new Set(state.aliveOrder.slice(0, MAX_ALIVE)),
  }), [state]);

  // 测试桥（E2E 冒烟用）——镜像 store 的**权威**状态。
  //
  // 为什么必须由 Provider 暴露，而不是让测试读 DOM / 读 localStorage：
  //   · DOM 不可靠：keep-alive(LRU) 下只有激活 tab 的 .wb-tabpane 不带 "inactive"
  //     （Workbench:172），所以「未 inactive 的 pane」恒等于 1 个 = 激活 tab；
  //     而它的 DOM 位置 = state.tabs 的下标，**与「最新使用的 tab」无关**。
  //     早期测试用「取最后一个可见 pane」是位置选择器，前提（最新 tab 在 tabs 末尾）
  //     根本不成立 → 断言指错（表现为 T22c 期望「设置」实得「行情分析」）。
  //   · localStorage 滞后：写盘是 200ms 防抖（见下方 useEffect），读到的可能是上一帧。
  //   · 原因根因：页面侧拿不到 store 闭包，测试只能从间接证据反推 —— 这里直接给出
  //     权威快照（激活 tab 的叶子标题 / 每个 tab 的标题 / 计数），换任意选择器都稳。
  useEffect(() => {
    if (typeof window === "undefined") return;
    const activeTab = state.tabs.find((t) => t.id === state.activeId) || state.tabs[0] || null;
    const leaves = (t) => {
      const out = [];
      (function walk(n) {
        if (!n || typeof n !== "object") return;
        if (n.pageKey) { out.push(n); return; }
        (n.kids || []).forEach(walk);
      })(t ? t.layout : null);
      return out;
    };
    const activeLeaf = activeTab
      ? (leaves(activeTab).find((l) => l.id === activeTab.activeLeaf) || leaves(activeTab)[0] || null)
      : null;
    const titleOf = (l) => {
      if (!l) return "";
      const base = (PAGES[l.pageKey] && PAGES[l.pageKey].label) || l.pageKey;
      const code = l.params && l.params.code;
      return code ? `${base} · ${code}` : base;
    };
    window.__qmtWorkspace = {
      tabCount: state.tabs.length,
      activeId: state.activeId,
      // 激活叶子标题：断言「当前前台看到的是哪一页」的唯一权威来源
      activeTitle: titleOf(activeLeaf),
      activePageKey: activeLeaf ? activeLeaf.pageKey : "",
      activeLeafId: activeLeaf ? activeLeaf.id : "",
      // 全部 tab：{ id, title, active }，用于诊断 tab 累积 / 单例聚焦行为
      tabs: state.tabs.map((t) => {
        const ls = leaves(t);
        const lf = ls.find((l) => l.id === t.activeLeaf) || ls[0] || null;
        return {
          id: t.id,
          title: titleOf(lf),
          leaves: ls.length,
          active: t.id === state.activeId,
        };
      }),
      aliveCount: state.aliveOrder.slice(0, MAX_ALIVE).length,
    };
  }, [state]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace() {
  return useContext(Ctx);
}

export function activeLeaves(tab) {
  if (!tab) return [];
  return collectLeaves(tab.layout);
}
