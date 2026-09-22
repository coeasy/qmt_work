import { describe, expect, it, beforeEach } from "vitest";
import fs from "node:fs";
import path from "node:path";

/**
 * 工作台 Tab 标题 —— 2026-09-20 第 17 轮补丁遗留项 ①。
 *
 * ## 缺陷
 * Tab 标题在 `open()` 那一刻就定死。从自选股点一只票跳工作台时，名称经常
 * **还没到位**（WS 还没推、REST 兜底还没回），于是标题被写成 `000001.SZ`；
 * 之后名称到了也**不会更新** —— 用户看到的是一个「永远叫代码的 Tab」。
 *
 * ## 修法
 * store 新增 `renameTab`，由**页面**在名称解析出来后主动改名（`PageProps` 已带
 * `tabId`，页面不需要去猜自己是哪个 Tab）。
 *
 * ## 这里锁的四条
 * ① 真名到位 ⇒ 标题跟着变；
 * ② 空标题**不改**（宁可留旧标题，也不能显示成一个空 Tab）；
 * ③ 幂等（同名不重写、不重复写盘）；
 * ④ 改名要持久化（刷新后不该退回代码）。
 */
// eslint-disable-next-line import/first
import { useWorkspaceStore } from "@/stores/workspace";

const SRC = path.resolve(__dirname, "../src");

beforeEach(() => {
  useWorkspaceStore.setState({
    tabs: [
      { id: "tab-a", title: "000001.SZ", tree: { kind: "leaf", id: "l1", pageKey: "workbench", params: { code: "000001.SZ" } }, activeLeafId: "l1" },
    ] as never,
    activeId: "tab-a",
    aliveOrder: ["tab-a"],
  });
});

describe("renameTab · Tab 标题跟随真名", () => {
  it("真名到位 ⇒ 标题跟着变（不再永远停在代码上）", () => {
    useWorkspaceStore.getState().renameTab("tab-a", "平安银行");
    expect(useWorkspaceStore.getState().tabs[0]?.title).toBe("平安银行");
  });

  it("空标题不改 —— 不能把 Tab 显示成空", () => {
    useWorkspaceStore.getState().renameTab("tab-a", "   ");
    expect(useWorkspaceStore.getState().tabs[0]?.title).toBe("000001.SZ");
  });

  it("幂等：同名不产生新数组（避免无意义重渲染）", () => {
    const before = useWorkspaceStore.getState().tabs;
    useWorkspaceStore.getState().renameTab("tab-a", "000001.SZ");
    expect(useWorkspaceStore.getState().tabs).toBe(before);
  });

  it("不存在的 tabId 静默忽略（不抛、不改其它 Tab）", () => {
    const before = useWorkspaceStore.getState().tabs;
    useWorkspaceStore.getState().renameTab("nope", "平安银行");
    expect(useWorkspaceStore.getState().tabs).toBe(before);
  });

  it("改名要持久化 —— 刷新后不该退回代码", () => {
    useWorkspaceStore.getState().renameTab("tab-a", "平安银行");
    const raw = localStorage.getItem("qmt.workspace.v1");
    expect(raw).toBeTruthy();
    expect(raw).toContain("平安银行");
  });
});

describe("工作台页必须自己补标题", () => {
  it("MarketWorkbench 用了 renameTab + useDisplayQuotes", () => {
    const src = fs.readFileSync(path.join(SRC, "domains/market/MarketWorkbench.tsx"), "utf8");
    expect(src, "未调用 renameTab ⇒ 标题会永久停在代码上").toContain("renameTab");
    // 头部价格也要走兜底，否则休市时工作台一片 --
    expect(src, "未使用 useDisplayQuotes ⇒ 休市时头部价格恒为 --").toContain(
      "useDisplayQuotes",
    );
  });
});
