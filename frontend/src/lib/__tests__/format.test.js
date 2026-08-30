import { describe, it, expect } from "vitest";
import { fmtAmount, fmtLimitDur, orderStatus } from "../format.js";

describe("fmtAmount", () => {
  it("空值 → —", () => {
    expect(fmtAmount(null)).toBe("—");
    expect(fmtAmount(undefined)).toBe("—");
  });
  it("亿档缩写", () => {
    expect(fmtAmount(2.5e8)).toBe("2.50亿");
    expect(fmtAmount(1.23e8)).toBe("1.23亿");
  });
  // 口径：亿/万档统一 2 位小数（此处曾为 .1f，与 useMarket.js 的 .2f 及
  // QuoteBoard/QuotePanel 的本地副本冲突，导致同一金额跨页面精度不一致）
  it("万档缩写", () => {
    expect(fmtAmount(5e4)).toBe("5.00万");
    expect(fmtAmount(12345)).toBe("1.23万");
  });
  it("<万取整", () => {
    expect(fmtAmount(9999)).toBe("9999");
    expect(fmtAmount(120)).toBe("120");
    expect(fmtAmount(12.7)).toBe("13");
  });
  // 回归：档位必须用绝对值判定。旧实现 `v >= 1e8` 会让负数金额（如资金净流出）
  // 漏判档位，直接输出 "-150000000" 这类原始数字。
  it("负数按绝对值取档（回归）", () => {
    expect(fmtAmount(-1.5e8)).toBe("-1.50亿");
    expect(fmtAmount(-2.5e4)).toBe("-2.50万");
    expect(fmtAmount(-8800)).toBe("-8800");
  });
  it("非法值 → —", () => {
    expect(fmtAmount(NaN)).toBe("—");
    expect(fmtAmount("abc")).toBe("—");
  });
});

describe("fmtLimitDur", () => {
  it("空值 → —", () => {
    expect(fmtLimitDur(0)).toBe("—");
    expect(fmtLimitDur(null)).toBe("—");
  });
  it("<60s 仅秒", () => {
    expect(fmtLimitDur(20)).toBe("20秒");
  });
  it("≥60s 分+秒", () => {
    expect(fmtLimitDur(200)).toBe("3分20秒");
  });
});

describe("orderStatus", () => {
  it("已知状态映射文案与样式", () => {
    expect(orderStatus("running")).toEqual({ label: "执行中", className: "run" });
    expect(orderStatus("done")).toEqual({ label: "已完成", className: "ok" });
    expect(orderStatus("failed")).toEqual({ label: "失败", className: "fail" });
    expect(orderStatus("part_filled")).toEqual({ label: "部成", className: "warn" });
  });
  it("未知状态回退原值", () => {
    expect(orderStatus("weird")).toEqual({ label: "weird", className: "" });
    expect(orderStatus(null)).toEqual({ label: "—", className: "" });
  });
});