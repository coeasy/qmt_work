// G11-4 虚拟列表窗口计算（纯函数，无 React 依赖）。
import { describe, expect, it } from "vitest";
import { computeVirtualWindow } from "../useVirtualList.js";

describe("computeVirtualWindow", () => {
  it("顶部窗口含 overscan", () => {
    const w = computeVirtualWindow(0, 280, 28, 8, 1000);
    expect(w.startIndex).toBe(0);
    expect(w.endIndex).toBeLessThanOrEqual(Math.ceil(280 / 28) + 8);
    expect(w.totalHeight).toBe(28000);
  });

  it("滚动后窗口平移且钳制末尾", () => {
    const w = computeVirtualWindow(28 * 500, 280, 28, 4, 1000);
    expect(w.startIndex).toBe(500 - 4);
    // 靠近末尾时不越界
    const tail = computeVirtualWindow(28 * 995, 280, 28, 4, 1000);
    expect(tail.endIndex).toBe(1000);
  });

  it("空数据/零高度安全", () => {
    expect(computeVirtualWindow(0, 0, 28, 8, 0).endIndex).toBe(0);
    expect(computeVirtualWindow(0, 280, 0, 8, 10).totalHeight).toBe(10);
  });
});
