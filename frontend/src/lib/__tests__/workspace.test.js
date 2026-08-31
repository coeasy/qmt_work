import { describe, expect, it } from "vitest";

import { makeLeaf, sanitizeTree } from "../../store/workspace.jsx";

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