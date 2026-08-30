import { describe, expect, it } from "vitest";

import { mainIndicatorOptions, subIndicatorOptions } from "../chartConfig.jsx";

// G9 chart-spec 消费辅助：选项映射（单一真源，消灭各页硬编码）
const SPEC = {
  candlestick: { up: "#ef4d56", down: "#29c08a" },
  main: { default: "ma", options: [
    { indicator: "ma", label: "MA", periods: [5, 10, 20, 60] },
    { indicator: "boll", label: "BOLL" },
  ] },
  sub: { default: "macd", options: [
    { indicator: "macd", label: "MACD" },
    { indicator: "kdj", label: "KDJ" },
    { indicator: "rsi", label: "RSI" },
    { indicator: "wr", label: "WR" },
  ] },
};

describe("chartConfig（G9 多图联动）", () => {
  it("mainIndicatorOptions：spec 选项 + 前置「无」", () => {
    const opts = mainIndicatorOptions(SPEC);
    expect(opts[0]).toEqual({ v: "none", label: "无" });
    expect(opts.map((o) => o.v)).toEqual(["none", "ma", "boll"]);
  });

  it("mainIndicatorOptions：spec 缺失 → 空数组（回退本地常量）", () => {
    expect(mainIndicatorOptions(null)).toEqual([{ v: "none", label: "无" }]);
  });

  it("subIndicatorOptions：spec 选项", () => {
    expect(subIndicatorOptions(SPEC).map((o) => o.v)).toEqual(["macd", "kdj", "rsi", "wr"]);
  });

  it("subIndicatorOptions：spec 缺失 → 空（回退本地常量）", () => {
    expect(subIndicatorOptions(undefined)).toEqual([]);
  });
});
