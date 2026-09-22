import { describe, expect, it, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";

/**
 * 「点一只股票 ⇒ 打开行情工作台」—— 第 17 轮需求 ② 的**覆盖面**锁。
 *
 * ## 为什么既有单测又有源码扫描
 * DataTable 走 `@tanstack/react-virtual`，jsdom 下容器高度恒 0 ⇒ **一行都不渲染**，
 * 所以「点列表跳工作台」这件事**没法靠点击断言**（这也是历史上多次把「页面有数据
 * 但显示空白」误判为通过的原因）。退而求其次：
 *   ① hook 行为用 `renderHook` 真测（改名后打开的是哪个页面、带什么参数）；
 *   ② 「哪些列表接了这一层」用源码扫描锁住，防止以后新增列表时漏接或各写一遍。
 */
// eslint-disable-next-line import/first
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
// eslint-disable-next-line import/first
import { useWorkspaceStore } from "@/stores/workspace";

const SRC = path.resolve(__dirname, "../src");

beforeEach(() => {
  useWorkspaceStore.setState({
    tabs: [
      {
        id: "t0",
        title: "仪表盘",
        tree: { kind: "leaf", id: "l0", pageKey: "dashboard", params: {} },
        activeLeafId: "l0",
      },
    ] as never,
    activeId: "t0",
    aliveOrder: ["t0"],
  });
});

describe("useOpenWorkbench · 唯一出口", () => {
  it("打开的是「行情工作台」（不是只开 K 线页）", () => {
    const { result } = renderHook(() => useOpenWorkbench());
    act(() => result.current("600519", "贵州茅台"));

    const tabs = useWorkspaceStore.getState().tabs;
    const last = tabs[tabs.length - 1] as unknown as {
      tree: { kind: string; pageKey: string; params: Record<string, unknown> };
    };
    expect(last.tree.pageKey).toBe("workbench");
    expect(last.tree.params.code).toBe("600519.SH");
    expect(last.tree.params.name).toBe("贵州茅台");
  });

  it("裸 6 位代码会被补上市场后缀（后端只认带后缀代码）", () => {
    const { result } = renderHook(() => useOpenWorkbench());
    act(() => result.current("000001"));
    const tabs = useWorkspaceStore.getState().tabs;
    const last = tabs[tabs.length - 1] as unknown as {
      tree: { params: Record<string, unknown> };
    };
    expect(last.tree.params.code).toBe("000001.SZ");
  });

  it("空代码不开页 —— 不开出一个「无标的」的空工作台", () => {
    const before = useWorkspaceStore.getState().tabs.length;
    const { result } = renderHook(() => useOpenWorkbench());
    act(() => result.current(""));
    expect(useWorkspaceStore.getState().tabs.length).toBe(before);
  });

  it("没传名称时标题先用代码（名称到位后由工作台 renameTab 改真名）", () => {
    const { result } = renderHook(() => useOpenWorkbench());
    act(() => result.current("000300.SH"));
    const tabs = useWorkspaceStore.getState().tabs;
    expect(tabs[tabs.length - 1]?.title).toBe("000300.SH");
  });
});

describe("各个列表都接了这一层", () => {
  // 「各个列表」是需求原文。这些文件里都有含 code 的行，点行必须能跳工作台。
  const LISTS: Array<[string, string]> = [
    ["domains/market/QuoteBoard.tsx", "自选股 / 报价牌"],
    ["shell/DataPanel.tsx", "侧栏自选股面板"],
    ["shell/MenuBar.tsx", "顶部搜索框"],
    ["domains/research/screen/AutoPicks.tsx", "选股结果"],
    ["domains/trading/LimitUp.tsx", "涨停监控"],
    ["domains/market/Etfs.tsx", "ETF 清单"],
    ["domains/account/Positions.tsx", "持仓 / 委托 / 成交"],
    ["domains/trading/Trade.tsx", "交易页持仓 / 委托 / 成交"],
  ];

  it.each(LISTS)("%s（%s）用 useOpenWorkbench，不自己拼 open()", (rel, _label) => {
    const src = fs.readFileSync(path.join(SRC, rel), "utf8");
    expect(src, `${rel} 未接入 useOpenWorkbench`).toContain("useOpenWorkbench");
    expect(src, `${rel} 未调用 openWorkbench`).toContain("openWorkbench(");
  });

  it("全站不得再有 open(\"quote\" 作为「点股票」的跳转目标", () => {
    // 「quote」是独立 K 线页。工作台内部的「独立打开 K 线」按钮用它是对的，
    // 所以只扫列表类文件 —— 它们在 LISTS 里已逐个断言过了。
    for (const [rel] of LISTS) {
      const src = fs.readFileSync(path.join(SRC, rel), "utf8");
      const hits = [...src.matchAll(/open\(\s*"quote"/g)].length;
      expect(hits, `${rel} 仍在把「点股票」跳到独立 K 线页`).toBe(0);
    }
  });
});
