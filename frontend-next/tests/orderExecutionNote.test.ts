import { describe, expect, it } from "vitest";
import { orderExecutionNote } from "@/domains/trading/OrderForm";

/**
 * 下单前的「这一单会不会真的报给券商」提示（2026-09-20 修复）。
 *
 * 修的是什么：提示只在 `!connected` 时渲染，且措辞把「没连券商」一律说成 503。
 * 两个后果：
 *   ① **已连券商 + 默认 paper 模式**（后端默认值就是 paper）这个最常见的新装组合下
 *      **一个字都不提示** —— 用户点「买入」以为下了实盘单，其实由本地 PaperEngine
 *      撮合，一根委托都没报给券商；而 Trade 页的持仓/委托/成交读的是券商接口，
 *      列表不会变，界面上完全看不出真相。
 *   ② 「未连接券商 ⇒ 下不了单」是错误归因：paper / dry_run 本来就不需要券商。
 *
 * 现在的契约：只要 mode !== "live" 就必须给出后果说明，**与是否连券商无关**。
 */
describe("orderExecutionNote", () => {
  it("★ 已连券商 + paper ⇒ 必须提示「不会向券商报单」（此前完全不提示）", () => {
    const n = orderExecutionNote("paper", true);
    expect(n).toBeTruthy();
    expect(n).toContain("模拟盘");
    expect(n).toContain("不会向券商报单");
  });

  it("未连券商 + paper ⇒ 提示本地撮合、不依赖券商（不得说成 503）", () => {
    const n = orderExecutionNote("paper", false) ?? "";
    expect(n).toContain("模拟盘");
    expect(n).toContain("不依赖券商");
    expect(n).not.toContain("503");
  });

  it("dry_run ⇒ 提示只返回计划，既不成交也不报单（与是否连券商无关）", () => {
    for (const connected of [true, false]) {
      const n = orderExecutionNote("dry_run", connected) ?? "";
      expect(n).toContain("预演");
      expect(n).toContain("不");
      expect(n).not.toContain("503");
    }
  });

  it("live + 已连券商 ⇒ 无提示（这是正常路径）", () => {
    expect(orderExecutionNote("live", true)).toBeNull();
  });

  it("live + 未连券商 ⇒ 提示 503 与连接引导", () => {
    const n = orderExecutionNote("live", false) ?? "";
    expect(n).toContain("503");
    expect(n).toContain("连接");
  });

  it("模式尚未取到（undefined）⇒ 未连券商时只说「取决于模式」，绝不断言后果", () => {
    const n = orderExecutionNote(undefined, false) ?? "";
    expect(n).toContain("取决于信号模式");
    expect(n).not.toContain("503");
    // 已连券商时不必打扰
    expect(orderExecutionNote(undefined, true)).toBeNull();
  });

  it("提示文案里不得出现 markdown 星号（JSX 不渲染 markdown，会显示成字面 **）", () => {
    for (const mode of ["paper", "dry_run", "live", undefined]) {
      for (const connected of [true, false]) {
        const n = orderExecutionNote(mode, connected) ?? "";
        expect(n).not.toContain("**");
      }
    }
  });
});
