import { describe, expect, it } from "vitest";

import { makeLeaf, sanitizeTree, workspaceReducer, MAX_ALIVE } from "../../store/workspace.jsx";

const seed = { pageKey: "dashboard", params: {} };

describe("阶段一：旧 key → 中心页 key 归一（makeLeaf）", () => {
  it("并入市场结构的旧 key → 归一为中心页 + 携带 tab=旧key", () => {
    const l = makeLeaf("boards");
    expect(l.pageKey).toBe("mktstructure");
    expect(l.params.tab).toBe("boards");
    expect(Object.keys(l.params)).toEqual(["tab"]);
  });
  it("并入自动化的旧 key → automation + tab 定位", () => {
    const l = makeLeaf("webhooks");
    expect(l.pageKey).toBe("automation");
    expect(l.params.tab).toBe("webhooks");
  });
  it("未归并的常规 key → 原样保留，不误加 tab", () => {
    const l = makeLeaf("trade", { code: "600519.SH" });
    expect(l.pageKey).toBe("trade");
    expect(l.params.tab).toBeUndefined();
    expect(l.params.code).toBe("600519.SH");
  });
  it("非法 key → 兜底默认页", () => {
    const l = makeLeaf("no-such-page");
    expect(l.pageKey).toBe("dashboard");
  });
});

describe("阶段一：持久化迁移（sanitizeTree 归一到中心页）", () => {
  it("旧 localStorage 里的叶子 key 升级为对应中心页 + tab", () => {
    const oldLeaf = { id: "l_old", pageKey: "reconcile", params: {} };
    const tree = sanitizeTree({ id: "root", dir: "h", kids: [oldLeaf] }, seed);
    // 归一后直接是全家小叶子（单子容器被提级）
    expect(tree.pageKey).toBe("audit_recon");
    expect(tree.params.tab).toBe("reconcile");
  });
  it("混合树：普通叶子不受影响", () => {
    const tree = sanitizeTree(
      { id: "root", dir: "h", kids: [
        { id: "a", pageKey: "quote", params: { code: "000001.SH" } },
        { id: "b", pageKey: "strategies", params: {} },
      ] },
      seed
    );
    const keys = tree.kids.map((k) => k.pageKey);
    expect(keys).toContain("quote");
    expect(keys).toContain("strategy_hub");
  });
});
/* ================= keep-alive LRU 语义（头部 = 最近使用） ================= */
// 回归：渲染层曾用 slice(-MAX_ALIVE) 取尾部，而激活把最新 tab 放头部，
// >8 个标签时刚激活的 tab 被挤出 alive 集合 → 内容区空白。
function mkState(n) {
  const tabs = Array.from({ length: n }, (_, i) => {
    const leaf = makeLeaf("dashboard");
    return { id: `t${i}`, layout: leaf, activeLeaf: leaf.id, pinned: false };
  });
  return { version: 3, tabs, activeId: tabs[0].id, aliveOrder: tabs.map((t) => t.id) };
}

function aliveIdsOf(state) {
  return new Set(state.aliveOrder.slice(0, MAX_ALIVE));
}

describe("keep-alive LRU：激活/关闭后激活 tab 必须在 alive 集合内", () => {
  it("超过 MAX_ALIVE 个标签时，激活最久未用的 tab 仍在 alive 集合头部", () => {
    let s = mkState(MAX_ALIVE + 2);            // 10 个 tab，t0..t9，t9 最久未用
    s = workspaceReducer(s, { type: "ACTIVATE", tabId: "t9" });
    expect(s.activeId).toBe("t9");
    expect(s.aliveOrder[0]).toBe("t9");
    expect(aliveIdsOf(s).has("t9")).toBe(true);
  });

  it("关闭激活 tab 后，新激活 tab 必须进入 alive 集合", () => {
    let s = mkState(MAX_ALIVE + 2);            // 激活 t0，关闭 t0 → 新激活 t1
    s = workspaceReducer(s, { type: "CLOSE_TAB", tabId: "t0" });
    expect(s.activeId).toBe("t1");
    expect(aliveIdsOf(s).has("t1")).toBe(true);
  });

  it("LRU 淘汰：激活后 aliveOrder 头部保留最近使用，渲染层只取头部 MAX_ALIVE", () => {
    let s = mkState(MAX_ALIVE + 2);
    s = workspaceReducer(s, { type: "ACTIVATE", tabId: "t5" });
    expect(s.aliveOrder.length).toBeLessThanOrEqual(MAX_ALIVE + 2);   // 存储层保留全量
    expect(s.aliveOrder[0]).toBe("t5");
    expect(aliveIdsOf(s).size).toBeLessThanOrEqual(MAX_ALIVE);        // 渲染层取头部 8 个
    expect(aliveIdsOf(s).has("t5")).toBe(true);
  });

  it("新开 tab（OPEN auto 兜底新窗）立即进入 alive 集合", () => {
    let s = mkState(3);
    s = workspaceReducer(s, { type: "OPEN", pageKey: "quote", params: { code: "600519.SH" }, openIn: "tab" });
    expect(s.tabs.length).toBe(4);
    const newest = s.tabs[s.tabs.length - 1].id;
    expect(s.activeId).toBe(newest);
    expect(aliveIdsOf(s).has(newest)).toBe(true);
  });
});
