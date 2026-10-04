/**
 * 文本兜底 `dashText` —— 「空串必须显示成 --，不能渲染成空白单元格」（2026-10-04 R25）。
 *
 * 为什么值得单独立一个文件：项目里 `x ?? "--"` 这种兜底写法有 31 处、`?? "—"` 52 处，
 * 而 `??` **只**拦 null/undefined，拦不住后端给的**空串**。于是「这一项本来没有」
 * 被渲染成了一个空白格子 —— 用户看到的是「前端坏了」。这类缺陷单看某一页很正常，
 * 只有横向比对才会发现是同一个语义错误，所以用一个共享函数 + 源码守卫统一掉。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { dashText } from "@/shared/format";

describe("dashText · 空值统一显示 -- （含空串）", () => {
  it("null / undefined → --", () => {
    expect(dashText(null)).toBe("--");
    expect(dashText(undefined)).toBe("--");
    // ★ 刻意**不**测「零参调用」：`v` 是必填参数，漏传应当被 tsc 拦下（TS2554）。
    //   若把签名改成可选来迁就测试，漏传参数就会静默变成 "--" —— 那是把缺陷藏起来。
  });

  it("空串 / 纯空白 → --（`??` 拦不住这一类）", () => {
    expect(dashText("")).toBe("--");
    expect(dashText("   ")).toBe("--");
    expect(dashText("\t\n")).toBe("--");
  });

  it("正常值原样返回，且去掉首尾空白", () => {
    expect(dashText("上交所")).toBe("上交所");
    expect(dashText(" 上交所 ")).toBe("上交所");
    expect(dashText("点")).toBe("点");
  });

  it("数字 0 必须保留为 \"0\"（`||` 会把 0 当成空）", () => {
    expect(dashText(0)).toBe("0");
    expect(dashText(0.5)).toBe("0.5");
  });
});

describe("单位/口径/交易所/类别列不得渲染空白单元格（源码级守卫）", () => {
  const FILES = [
    "domains/market/MarketStructure.tsx",   // 口径 / 单位
    "domains/market/SectorRadar.tsx",       // 交易所 / 类别
    "domains/market/Etfs.tsx",              // 交易所
    "domains/research/Search.tsx",          // 交易所 / 类别
  ];

  it("这些文件都从 shared/format 引入 dashText", () => {
    for (const f of FILES) {
      const src = readFileSync(resolve(__dirname, "../src", f), "utf8");
      expect(src, `${f} 未引入 dashText`).toContain("dashText");
    }
  });

  it("不得再出现对 unit / metric / exchange / category 的 `?? \"--\"` 兜底", () => {
    // 这些字段后端会给**空串**（板块 unit 曾是 ""），`??` 兜不住 ⇒ 空白单元格。
    const bad = [
      /r\.unit \?\? "--"/,
      /r\.metric \?\? "--"/,
      /r\.exchange \?\? "--"/,
      /r\.category \?\? "--"/,
    ];
    for (const f of FILES) {
      const src = readFileSync(resolve(__dirname, "../src", f), "utf8");
      for (const re of bad) {
        expect(re.test(src), `${f} 仍存在 ${re}（对空串无效）`).toBe(false);
      }
    }
  });
});
