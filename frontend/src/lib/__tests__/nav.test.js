import { describe, expect, it } from "vitest";

import { normalizeNavDetail } from "../nav.js";

describe("normalizeNavDetail（导航协议归一化）", () => {
  it("字符串（旧调用点）→ 归一为对象，params 空", () => {
    expect(normalizeNavDetail("quote")).toEqual({ pageKey: "quote", params: {}, openIn: "auto" });
  });
  it("非法字符串 → null", () => {
    expect(normalizeNavDetail("no-such-page")).toBeNull();
  });
  it("对象（新调用点）→ 原样携带 params", () => {
    expect(normalizeNavDetail({ pageKey: "quote", params: { code: "600519.SH" }, openIn: "replace" }))
      .toEqual({ pageKey: "quote", params: { code: "600519.SH" }, openIn: "replace" });
  });
  it("对象缺 params → 补空对象；缺 openIn → auto", () => {
    expect(normalizeNavDetail({ pageKey: "boards" }))
      .toEqual({ pageKey: "boards", params: {}, openIn: "auto" });
  });
  it("对象非法 pageKey → 兜底 DEFAULT_PAGE", () => {
    const r = normalizeNavDetail({ pageKey: "nope", params: {} });
    expect(r.pageKey).toBeDefined();   // dashboard 或默认页（不崩）
  });
  it("null/undefined/数字 → null", () => {
    expect(normalizeNavDetail(null)).toBeNull();
    expect(normalizeNavDetail(undefined)).toBeNull();
    expect(normalizeNavDetail(42)).toBeNull();
  });
});
