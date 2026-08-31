import { describe, expect, it } from "vitest";

import { TYPE_BADGE, badgeOf } from "../instrument.js";

// 标的类型徽章（与后端 instrument.py 分类对应）：ETF 蓝 / 股 灰 / 指 紫 / 板块 橙 / 债 青
describe("badgeOf（标的类型徽章）", () => {
  it("五种已知类型 → 对应标签与样式类", () => {
    expect(badgeOf("stock")).toEqual({ label: "股", cls: "stock" });
    expect(badgeOf("etf")).toEqual({ label: "ETF", cls: "etf" });
    expect(badgeOf("index")).toEqual({ label: "指", cls: "index" });
    expect(badgeOf("board")).toEqual({ label: "板块", cls: "board" });
    expect(badgeOf("bond")).toEqual({ label: "债", cls: "bond" });
  });
  it("未知 / 空 / null → 兜底「标的」", () => {
    expect(badgeOf("unknown")).toEqual({ label: "标的", cls: "other" });
    expect(badgeOf("")).toEqual({ label: "标的", cls: "other" });
    expect(badgeOf(null)).toEqual({ label: "标的", cls: "other" });
    expect(badgeOf(undefined)).toEqual({ label: "标的", cls: "other" });
  });
  it("徽章表覆盖后端 instrument.py 全部类型", () => {
    // 后端 classify_instrument 的 type 枚举：stock/etf/index/board/bond（unknown 走兜底）
    expect(Object.keys(TYPE_BADGE).sort()).toEqual(["board", "bond", "etf", "index", "stock"]);
  });
});
