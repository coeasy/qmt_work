import { beforeEach, describe, expect, it } from "vitest";

import { api } from "../../api.js";
import { _resetIndicatorCache, fetchIndicator, fetchIndicators, indicatorKey } from "../indicators.js";

// G2-3 缓存层契约：同键只请求一次 / 503 → null 不本地重算 / 键排序稳定
beforeEach(() => {
  _resetIndicatorCache();   // 清模块级缓存，保证用例隔离
});

describe("fetchIndicator（指标缓存层）", () => {
  it("首次请求命中后端，同键二次不再请求（缓存）", async () => {
    let calls = 0;
    api.marketIndicatorsCalc = async () => { calls += 1; return { outputs: { k: [1, 2] } }; };
    const a = await fetchIndicator("kdj", { code: "600519.SH", n: 9 });
    const b = await fetchIndicator("kdj", { code: "600519.SH", n: 9 });
    expect(a).toEqual({ k: [1, 2] });
    expect(b).toEqual({ k: [1, 2] });
    expect(calls).toBe(1);
  });

  it("不同参数（n 不同）→ 不同缓存键，重新请求", async () => {
    let calls = 0;
    api.marketIndicatorsCalc = async () => { calls += 1; return { outputs: {} }; };
    await fetchIndicator("kdj", { code: "600519.SH", n: 9 });
    await fetchIndicator("kdj", { code: "600519.SH", n: 14 });
    expect(calls).toBe(2);
  });

  it("后端 503/异常 → 返回 null（不本地重算）", async () => {
    api.marketIndicatorsCalc = async () => { throw new Error("503"); };
    const r = await fetchIndicator("macd", { code: "600519.SH" });
    expect(r).toBeNull();
  });

  it("参数顺序不影响缓存键（键稳定性）", async () => {
    let calls = 0;
    api.marketIndicatorsCalc = async () => { calls += 1; return { outputs: {} }; };
    await fetchIndicator("ma", { code: "600519.SH", win: 20, period: "1d" });
    await fetchIndicator("ma", { period: "1d", win: 20, code: "600519.SH" });
    expect(calls).toBe(1);
  });
});

describe("fetchIndicators / indicatorKey", () => {
  it("批量并行 + 结果按 key 索引（与 fetchIndicator 缓存键一致）", async () => {
    api.marketIndicatorsCalc = async (p) => ({ outputs: { [p.name]: [p.name] } });
    const map = await fetchIndicators([
      ["macd", { code: "600519.SH" }],
      ["rsi", { code: "600519.SH", win: 6 }],
    ]);
    expect(map[indicatorKey("macd", { code: "600519.SH" })]).toEqual({ macd: ["macd"] });
    expect(map[indicatorKey("rsi", { code: "600519.SH", win: 6 })]).toEqual({ rsi: ["rsi"] });
    // 与 fetchIndicator 同键 → 二次请求直接命中缓存
    let calls = 0;
    api.marketIndicatorsCalc = async (p) => { calls += 1; return { outputs: { [p.name]: [] } }; };
    await fetchIndicator("macd", { code: "600519.SH" });
    expect(calls).toBe(0);
  });
});
