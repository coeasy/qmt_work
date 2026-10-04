import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

/**
 * 「基本信息 + 基本面」= 一块**资料面板**（Tab 切换）在右栏 —— 2026-10-03 第 2 轮布局。
 *
 * ## 为什么合并成一块
 * 二者回答的是同一个问题（「这只票是什么」），此前在界面上是两块独立 Panel：
 *   ① 各占一个标题栏与内边距，300px 宽的右栏光标题就吃掉可观高度；
 *   ② 「基本面」在底部坞、**要展开才看得见**，而「市值/PE/涨跌停」是看盘时随时
 *      要瞄一眼的。合并成 Tab 后同样的高度能多显示数据，且两者都不用展开。
 *
 * ## 为什么中列底部坞整体去掉
 * 底部坞的存在前提是「基本面只能放在中列」—— 基本面已经进了右栏「资料」，
 * 底部坞便只剩下一块与右上「下单」重复的下单区，等于用 42% 的图表高度换重复内容。
 * 去掉后 K 线 / 分时**吃满整列高度**（看盘页的主角是图）。
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

describe("右栏「资料」= 基本信息 | 基本面；底部坞已整体移除", () => {
  const wb = () => code("domains/market/MarketWorkbench.tsx");

  it("工作台同时渲染 StockInfoPanel 与 FundamentalsPanel（同一块「资料」下）", () => {
    const src = wb();
    expect(src, "工作台不再渲染基本信息面板").toContain("StockInfoPanel");
    expect(src, "工作台不再渲染基本面面板").toContain("FundamentalsPanel");
  });

  it("两者合并为一块 Tab 面板：info / fundamentals 两个页签，靠 infoTab 切换", () => {
    const src = wb();
    expect(src, '资料面板缺「基本信息」页签').toContain('key: "info"');
    expect(src, '资料面板缺「基本面」页签').toContain('key: "fundamentals"');
    expect(src, "两个页签不是同一个 state（infoTab）驱动").toContain("infoTab");
    // 只能是 infoTab 一个开关，不得再各自维护展开态（否则又变成两块）
    expect(src, "仍存在 infoOpen 之外的第二套展开态").toContain("infoOpen");
  });

  it("★ 中列底部坞已移除 —— 图表吃满整列高度", () => {
    const src = wb();
    expect(src, "Dock 类型还在（底部坞未移除）").not.toMatch(/type Dock\b/);
    expect(src, "仍在渲染 dock 面板").not.toMatch(/\bdock ===/);
    expect(src, "仍在使用底部坞样式 s.dock").not.toMatch(/s\.dock\b/);
  });

  it("面板把扩展字段都渲染出来（市值 / 估值 / 换手…）", () => {
    const src = code("domains/market/panels/StockInfoPanel.tsx");
    for (const key of ["total_mv", "circ_mv", "pe_ttm", "pb", "turnover_rate",
                       "amplitude", "volume_ratio", "avg_price"]) {
      expect(src, `基本信息面板少了字段 ${key}`).toContain(key);
    }
  });
});
