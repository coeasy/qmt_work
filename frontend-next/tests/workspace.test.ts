import { describe, expect, it, beforeEach } from "vitest";
import {
  MAX_ALIVE,
  collectLeaves,
  leafCount,
  newLeaf,
  useWorkspaceStore,
} from "@/stores/workspace";

/**
 * 工作区 store 测试：多 Tab / LRU keep-alive / 分栏 / 比例持久化。
 * 这些行为是「专业客户端级多窗口体验」的基础，必须可回归。
 */

function reset(): void {
  const st = useWorkspaceStore.getState();
  // 关掉除首个以外的全部 Tab，回到干净状态
  const tabs = useWorkspaceStore.getState().tabs;
  for (const t of tabs.slice(1)) st.close(t.id);
}

describe("workspace store", () => {
  beforeEach(reset);

  it("open 会新建 Tab 并激活", () => {
    const before = useWorkspaceStore.getState().tabs.length;
    useWorkspaceStore.getState().open("quoteboard", {}, { title: "报价牌" });
    const st = useWorkspaceStore.getState();
    expect(st.tabs.length).toBe(before + 1);
    const active = st.tabs.find((t) => t.id === st.activeId);
    expect(active?.title).toBe("报价牌");
  });

  it("相同 pageKey+params 默认聚焦复用而非重复开 Tab", () => {
    const st = () => useWorkspaceStore.getState();
    st().open("quote", { code: "600519.SH" }, { title: "贵州茅台" });
    const n = st().tabs.length;
    st().open("quote", { code: "600519.SH" }, { title: "贵州茅台" });
    expect(st().tabs.length).toBe(n);

    // reuse: "new" 则强制新开
    st().open("quote", { code: "600519.SH" }, { title: "贵州茅台", reuse: "new" });
    expect(st().tabs.length).toBe(n + 1);
  });

  it("LRU：最近使用的 Tab 排在最前，超出 MAX_ALIVE 的不在活跃集合内", () => {
    const st = () => useWorkspaceStore.getState();
    const first = st().tabs[0]!.id;

    for (let i = 0; i < MAX_ALIVE + 3; i++) {
      st().open("quoteboard", { n: i }, { title: `T${i}`, reuse: "new" });
    }

    // 最新打开的在最前
    expect(st().aliveOrder[0]).toBe(st().activeId);

    // 最早的 Tab 已被挤出活跃集合
    const alive = st().aliveOrder.slice(0, MAX_ALIVE);
    expect(alive).not.toContain(first);

    // 但重新激活后应回到活跃集合头部
    st().activate(first);
    expect(st().aliveOrder.slice(0, MAX_ALIVE)).toContain(first);
  });

  it("close 会移除 Tab，并在关闭当前 Tab 时切到相邻 Tab", () => {
    const st = () => useWorkspaceStore.getState();
    st().open("quoteboard", {}, { title: "A", reuse: "new" });
    const second = st().activeId;
    st().open("watchlist", {}, { title: "B", reuse: "new" });

    st().close(second);
    expect(st().tabs.some((t) => t.id === second)).toBe(false);
    expect(st().tabs.some((t) => t.id === st().activeId)).toBe(true);
  });

  it("分栏：splitLeaf 增加叶子，closeLeaf 减少叶子", () => {
    const st = () => useWorkspaceStore.getState();
    const tab = st().tabs.find((t) => t.id === st().activeId)!;
    expect(leafCount(tab.tree)).toBe(1);

    st().splitLeaf(tab.id, tab.activeLeafId, "h", "quote");
    const after = st().tabs.find((t) => t.id === tab.id)!;
    expect(leafCount(after.tree)).toBe(2);

    const leaves = collectLeaves(after.tree);
    expect(leaves.length).toBe(2);

    st().closeLeaf(tab.id, leaves[1]!.id);
    const final = st().tabs.find((t) => t.id === tab.id)!;
    expect(leafCount(final.tree)).toBe(1);
  });

  it("分栏比例被限制在 0.1~0.9，避免窗格被拖到不可见", () => {
    const st = () => useWorkspaceStore.getState();
    const tab = st().tabs.find((t) => t.id === st().activeId)!;
    st().splitLeaf(tab.id, tab.activeLeafId, "v", "quote");

    const withSplit = st().tabs.find((t) => t.id === tab.id)!;
    const split = collectLeaves(withSplit.tree).length === 2 ? withSplit.tree : null;
    expect(split?.kind).toBe("split");
    if (split?.kind !== "split") return;

    st().setRatio(tab.id, split.id, 0.01);
    let tree = st().tabs.find((t) => t.id === tab.id)!.tree;
    expect(tree.kind === "split" && tree.ratio).toBe(0.1);

    st().setRatio(tab.id, split.id, 0.99);
    tree = st().tabs.find((t) => t.id === tab.id)!.tree;
    expect(tree.kind === "split" && tree.ratio).toBe(0.9);
  });

  it("关闭最后一个叶子会连带关闭整个 Tab", () => {
    const st = () => useWorkspaceStore.getState();
    st().open("quoteboard", {}, { title: "only", reuse: "new" });
    const tabId = st().activeId;
    const tab = st().tabs.find((t) => t.id === tabId)!;

    st().closeLeaf(tabId, tab.activeLeafId);
    expect(st().tabs.some((t) => t.id === tabId)).toBe(false);
  });

  it("newLeaf 生成唯一 id", () => {
    const a = newLeaf("quote");
    const b = newLeaf("quote");
    expect(a.kind).toBe("leaf");
    expect(b.kind).toBe("leaf");
    expect(a.id).not.toBe(b.id);
  });
});
