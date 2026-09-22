import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

/**
 * 「基本信息」的位置锁 —— 2026-09-21：从底部坞挪到**右栏常驻**。
 *
 * ## 为什么挪
 * 底部坞要**先展开**才看得见，而「这只票市值多少、PE 多少、涨跌停在哪」是看盘时
 * 随时要瞄一眼的 —— 放在右栏才能「快速查看」。
 *
 * ## 为什么不能有第二处
 * 挪完之后底部坞若还留着「基本信息」页签，就又是「同一份数据两处展示」
 * （与「自选股左右各一份」同一类问题）：两处各自拉一次接口、各自维护加载态，
 * 改一处另一处不跟随。
 *
 * ⚠️ 与 `watchlistPlacement.test.ts` 同样的坑：**必须先剥注释再扫** ——
 *    源码注释里会提到「基本信息曾经在底部坞」，全文匹配会把说明判成「还在」。
 */
const SRC = path.resolve(__dirname, "../src");
const read = (rel: string) => fs.readFileSync(path.join(SRC, rel), "utf8");
const code = (rel: string) =>
  read(rel)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/[^\n]*/g, "$1");

describe("基本信息在右栏常驻，底部坞不再重复一份", () => {
  const wb = () => code("domains/market/MarketWorkbench.tsx");

  it("行情工作台渲染 StockInfoPanel（右栏常驻）", () => {
    expect(wb(), "工作台不再渲染基本信息面板").toContain("StockInfoPanel");
  });

  it("底部坞不再有「基本信息」页签 —— 不重复一份", () => {
    const src = wb();
    expect(src, '底部坞仍有 key: "info" 页签').not.toContain('key: "info"');
    expect(src, "底部坞仍在渲染 dock === \"info\"").not.toContain('dock === "info"');
    expect(src, "Dock 类型仍含 info").not.toMatch(/type Dock = [^;]*"info"/);
  });

  it("基本信息面板支持折叠（右栏要留给成交流与下单）", () => {
    expect(wb(), "没有折叠开关").toContain("infoOpen");
  });

  it("面板把扩展字段都渲染出来（市值 / 估值 / 换手…）", () => {
    const src = code("domains/market/panels/StockInfoPanel.tsx");
    for (const key of ["total_mv", "circ_mv", "pe_ttm", "pb", "turnover_rate",
                       "amplitude", "volume_ratio", "avg_price"]) {
      expect(src, `基本信息面板少了字段 ${key}`).toContain(key);
    }
  });
});
