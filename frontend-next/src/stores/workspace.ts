import { create } from "zustand";

/**
 * 工作区 store：多 Tab + LRU keep-alive + 可拖拽分栏。
 *
 * 与旧前端的差异：
 * - 旧实现非激活 Tab 用 visibility:hidden 常驻 DOM，且仅 LRU 逐出时才卸载，
 *   逐出后本地 UI state（滚动/输入/画线）全部丢失；
 *   本实现显式区分「已打开」与「活跃渲染」两个集合，逐出策略可配置，
 *   并为每个叶子保留 params 快照以便恢复。
 * - 分栏比例可拖拽并持久化（旧实现固定 flex:1 等分）。
 */

export type SplitDir = "h" | "v";

export type PaneNode =
  | { kind: "leaf"; id: string; pageKey: string; params: Record<string, unknown> }
  | { kind: "split"; id: string; dir: SplitDir; ratio: number; a: PaneNode; b: PaneNode };

export interface Tab {
  id: string;
  title: string;
  tree: PaneNode;
  activeLeafId: string;
}

/** 同时保持挂载（keep-alive）的最大 Tab 数，超出者卸载但保留在状态中 */
export const MAX_ALIVE = 8;
/** 最大打开 Tab 数 */
export const MAX_TABS = 24;
/** 单个 Tab 内最大叶子数（分栏上限） */
export const MAX_LEAVES = 4;

const STORAGE_KEY = "qmt.workspace.v1";

let uid = 0;
const nextId = (prefix: string): string => {
  uid += 1;
  return `${prefix}-${Date.now().toString(36)}-${uid}`;
};

export const newLeaf = (
  pageKey: string,
  params: Record<string, unknown> = {},
): PaneNode => ({ kind: "leaf", id: nextId("leaf"), pageKey, params });

function leafCount(node: PaneNode): number {
  return node.kind === "leaf" ? 1 : leafCount(node.a) + leafCount(node.b);
}

function collectLeaves(node: PaneNode, out: PaneNode[] = []): PaneNode[] {
  if (node.kind === "leaf") out.push(node);
  else {
    collectLeaves(node.a, out);
    collectLeaves(node.b, out);
  }
  return out;
}

/** 用 mapper 替换目标叶子；返回新树（不可变更新） */
function mapLeaf(
  node: PaneNode,
  leafId: string,
  mapper: (leaf: Extract<PaneNode, { kind: "leaf" }>) => PaneNode,
): PaneNode {
  if (node.kind === "leaf") {
    return node.id === leafId ? mapper(node) : node;
  }
  return { ...node, a: mapLeaf(node.a, leafId, mapper), b: mapLeaf(node.b, leafId, mapper) };
}

/** 移除叶子；若兄弟节点成为孤儿则提级 */
function removeLeaf(node: PaneNode, leafId: string): PaneNode | null {
  if (node.kind === "leaf") return node.id === leafId ? null : node;
  const a = removeLeaf(node.a, leafId);
  const b = removeLeaf(node.b, leafId);
  if (a === null) return b;
  if (b === null) return a;
  return { ...node, a, b };
}

function setRatio(node: PaneNode, splitId: string, ratio: number): PaneNode {
  if (node.kind === "leaf") return node;
  if (node.id === splitId) return { ...node, ratio: clamp(ratio, 0.1, 0.9) };
  return { ...node, a: setRatio(node.a, splitId, ratio), b: setRatio(node.b, splitId, ratio) };
}

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

function firstLeafId(node: PaneNode): string {
  return node.kind === "leaf" ? node.id : firstLeafId(node.a);
}

export interface OpenOptions {
  /** 标题（默认取页面注册表） */
  title?: string;
  /** 同 pageKey 已存在时的行为：focus 复用（默认）/ new 强制新开 */
  reuse?: "focus" | "new";
}

export interface WorkspaceState {
  tabs: Tab[];
  activeId: string;
  /** LRU：最近使用在前 */
  aliveOrder: string[];

  open: (pageKey: string, params?: Record<string, unknown>, opts?: OpenOptions) => void;
  /**
   * 改名 —— 页面**自己**把标题补成真实名称。
   *
   * ★ 为什么需要（2026-09-20 实测）：Tab 标题在 `open()` 那一刻就定死了。
   * 从列表点一只股票跳工作台时，名称可能还没到位（WS 还没推、REST 兜底还没回），
   * 于是标题永久停在 `000001.SZ` —— 之后名称到了也**不会变**，用户看到的
   * 是一个「永远叫代码的 Tab」。改名只能由页面在名称解析出来后主动发起。
   *
   * 空标题直接忽略：宁可保留旧标题，也不把标题改成空串（那会显示成一个空 Tab）。
   */
  renameTab: (tabId: string, title: string) => void;
  close: (tabId: string) => void;
  closeOthers: (tabId: string) => void;
  /** 关闭该 Tab 右侧的全部 Tab（保留它本身与左侧的） */
  closeRight: (tabId: string) => void;
  activate: (tabId: string) => void;
  moveTab: (from: number, to: number) => void;

  splitLeaf: (tabId: string, leafId: string, dir: SplitDir, pageKey?: string) => void;
  closeLeaf: (tabId: string, leafId: string) => void;
  setActiveLeaf: (tabId: string, leafId: string) => void;
  setRatio: (tabId: string, splitId: string, ratio: number) => void;
  setLeafParams: (tabId: string, leafId: string, params: Record<string, unknown>) => void;

  /** 当前应保持挂载的 Tab（LRU 前 N） */
  aliveIds: () => string[];
  hydrate: () => void;
}

function touch(order: string[], id: string): string[] {
  return [id, ...order.filter((x) => x !== id)];
}

function persist(s: Pick<WorkspaceState, "tabs" | "activeId" | "aliveOrder">): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ tabs: s.tabs, activeId: s.activeId, aliveOrder: s.aliveOrder }),
    );
  } catch {
    /* 存储不可用时静默降级 */
  }
}

const initialTab: Tab = {
  id: nextId("tab"),
  title: "仪表盘",
  tree: newLeaf("dashboard"),
  activeLeafId: "",
};
initialTab.activeLeafId = firstLeafId(initialTab.tree);

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  tabs: [initialTab],
  activeId: initialTab.id,
  aliveOrder: [initialTab.id],

  open(pageKey, params = {}, opts = {}) {
    const { tabs, aliveOrder } = get();
    const reuse = opts.reuse ?? "focus";
    const title = opts.title ?? pageKey;

    if (reuse === "focus") {
      const found = tabs.find(
        (t) =>
          t.tree.kind === "leaf" &&
          t.tree.pageKey === pageKey &&
          JSON.stringify(t.tree.params) === JSON.stringify(params),
      );
      if (found) {
        set({ activeId: found.id, aliveOrder: touch(aliveOrder, found.id) });
        const next = { tabs, activeId: found.id, aliveOrder: touch(aliveOrder, found.id) };
        persist(next);
        return;
      }
    }

    let nextTabs = tabs;
    if (tabs.length >= MAX_TABS) {
      // 淘汰最久未使用且非当前的 Tab
      const victim = [...aliveOrder].reverse().find((id) => id !== get().activeId);
      if (victim) nextTabs = tabs.filter((t) => t.id !== victim);
    }

    const tab: Tab = {
      id: nextId("tab"),
      title,
      tree: newLeaf(pageKey, params),
      activeLeafId: "",
    };
    tab.activeLeafId = firstLeafId(tab.tree);

    const tabsOut = [...nextTabs, tab];
    const orderOut = touch(aliveOrder, tab.id);
    set({ tabs: tabsOut, activeId: tab.id, aliveOrder: orderOut });
    persist({ tabs: tabsOut, activeId: tab.id, aliveOrder: orderOut });
  },

  renameTab(tabId, title) {
    const { tabs, activeId, aliveOrder } = get();
    const next = String(title ?? "").trim();
    if (!next) return; // 空标题不改：宁可留旧标题，也不显示成空 Tab
    const cur = tabs.find((t) => t.id === tabId);
    if (!cur || cur.title === next) return; // 幂等：避免无意义的重渲染 / 写盘
    const tabsOut = tabs.map((t) => (t.id === tabId ? { ...t, title: next } : t));
    set({ tabs: tabsOut });
    persist({ tabs: tabsOut, activeId, aliveOrder });
  },

  close(tabId) {
    const { tabs, activeId, aliveOrder } = get();
    const idx = tabs.findIndex((t) => t.id === tabId);
    if (idx < 0) return;
    const tabsOut = tabs.filter((t) => t.id !== tabId);
    let activeOut = activeId;
    if (activeId === tabId) {
      const fallback = tabsOut[Math.min(idx, tabsOut.length - 1)];
      activeOut = fallback?.id ?? "";
    }
    const orderOut = aliveOrder.filter((x) => x !== tabId);
    set({ tabs: tabsOut, activeId: activeOut, aliveOrder: orderOut });
    persist({ tabs: tabsOut, activeId: activeOut, aliveOrder: orderOut });
  },

  closeOthers(tabId) {
    const { tabs } = get();
    const keep = tabs.filter((t) => t.id === tabId);
    if (keep.length === 0) return;
    set({ tabs: keep, activeId: tabId, aliveOrder: [tabId] });
    persist({ tabs: keep, activeId: tabId, aliveOrder: [tabId] });
  },

  closeRight(tabId) {
    const { tabs, activeId, aliveOrder } = get();
    const idx = tabs.findIndex((t) => t.id === tabId);
    if (idx < 0) return;
    const keep = tabs.slice(0, idx + 1);
    const keepIds = new Set(keep.map((t) => t.id));
    // 被关掉的若正好是当前激活页，回退到右键点的那个（用户视线本来就在它上面）
    const activeOut = keepIds.has(activeId) ? activeId : tabId;
    const orderOut = aliveOrder.filter((x) => keepIds.has(x));
    set({ tabs: keep, activeId: activeOut, aliveOrder: orderOut });
    persist({ tabs: keep, activeId: activeOut, aliveOrder: orderOut });
  },

  activate(tabId) {
    const { tabs, aliveOrder } = get();
    if (!tabs.some((t) => t.id === tabId)) return;
    const orderOut = touch(aliveOrder, tabId);
    set({ activeId: tabId, aliveOrder: orderOut });
    persist({ tabs, activeId: tabId, aliveOrder: orderOut });
  },

  moveTab(from, to) {
    const { tabs } = get();
    if (from < 0 || to < 0 || from >= tabs.length || to >= tabs.length) return;
    const out = [...tabs];
    const [moved] = out.splice(from, 1);
    if (!moved) return;
    out.splice(to, 0, moved);
    set({ tabs: out });
    persist({ tabs: out, activeId: get().activeId, aliveOrder: get().aliveOrder });
  },

  splitLeaf(tabId, leafId, dir, pageKey) {
    const { tabs } = get();
    const tab = tabs.find((t) => t.id === tabId);
    if (!tab) return;
    if (leafCount(tab.tree) >= MAX_LEAVES) return;
    const nextPage = pageKey ?? "dashboard";
    const out = tabs.map((t) => {
      if (t.id !== tabId) return t;
      const tree = mapLeaf(t.tree, leafId, (leaf) => ({
        kind: "split",
        id: nextId("split"),
        dir,
        ratio: 0.5,
        a: leaf,
        b: newLeaf(nextPage),
      }));
      // 分栏后焦点保持在原叶子，避免用户视线跳动
      return { ...t, tree, activeLeafId: t.activeLeafId };
    });
    set({ tabs: out });
    persist({ tabs: out, activeId: get().activeId, aliveOrder: get().aliveOrder });
  },

  closeLeaf(tabId, leafId) {
    const { tabs } = get();
    const out: Tab[] = [];
    for (const t of tabs) {
      if (t.id !== tabId) {
        out.push(t);
        continue;
      }
      const tree = removeLeaf(t.tree, leafId);
      if (tree === null) continue; // 最后一个叶子被关 → 整个 Tab 关闭
      out.push({
        ...t,
        tree,
        activeLeafId: t.activeLeafId === leafId ? firstLeafId(tree) : t.activeLeafId,
      });
    }
    if (out.length === 0) return;
    const activeOut = out.some((t) => t.id === get().activeId) ? get().activeId : out[0]!.id;
    const orderOut = get().aliveOrder.filter((id) => out.some((t) => t.id === id));
    set({ tabs: out, activeId: activeOut, aliveOrder: orderOut });
    persist({ tabs: out, activeId: activeOut, aliveOrder: orderOut });
  },

  setActiveLeaf(tabId, leafId) {
    const out = get().tabs.map((t) => (t.id === tabId ? { ...t, activeLeafId: leafId } : t));
    set({ tabs: out });
  },

  setRatio(tabId, splitId, ratio) {
    const out = get().tabs.map((t) =>
      t.id === tabId ? { ...t, tree: setRatio(t.tree, splitId, ratio) } : t,
    );
    set({ tabs: out });
    persist({ tabs: out, activeId: get().activeId, aliveOrder: get().aliveOrder });
  },

  setLeafParams(tabId, leafId, params) {
    const out = get().tabs.map((t) =>
      t.id === tabId
        ? { ...t, tree: mapLeaf(t.tree, leafId, (leaf) => ({ ...leaf, params })) }
        : t,
    );
    set({ tabs: out });
  },

  aliveIds() {
    return get().aliveOrder.slice(0, MAX_ALIVE);
  },

  hydrate() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Partial<WorkspaceState>;
      if (!Array.isArray(parsed.tabs) || parsed.tabs.length === 0) return;
      const tabs = parsed.tabs.filter(
        (t): t is Tab =>
          !!t && typeof t.id === "string" && !!t.tree && typeof t.activeLeafId === "string",
      );
      if (tabs.length === 0) return;
      const activeId =
        typeof parsed.activeId === "string" && tabs.some((t) => t.id === parsed.activeId)
          ? parsed.activeId
          : tabs[0]!.id;
      const aliveOrder = Array.isArray(parsed.aliveOrder)
        ? parsed.aliveOrder.filter((id) => tabs.some((t) => t.id === id))
        : [activeId];
      set({ tabs, activeId, aliveOrder: aliveOrder.length > 0 ? aliveOrder : [activeId] });
    } catch {
      /* 脏数据忽略，保留默认工作区 */
    }
  },
}));

export { leafCount, collectLeaves, firstLeafId };
