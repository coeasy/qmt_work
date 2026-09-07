import { describe, expect, it } from "vitest";

import { explainTradeError, normalizeOrderPayload, precheckOrder, sendOrder } from "../tradeApi.js";
import { api } from "../../api.js";

describe("normalizeOrderPayload（交易字段规整）", () => {
  it("空参数 → 抛错", () => {
    expect(() => normalizeOrderPayload()).toThrow();
    expect(() => normalizeOrderPayload(null)).toThrow();
    expect(() => normalizeOrderPayload({})).toThrow("代码不能为空");
  });
  it("side 兼容 → direction；代码大写规整", () => {
    const r = normalizeOrderPayload({ code: "sh600519", side: "buy", volume: 100, price: 1500.123 });
    expect(r.direction).toBe("buy");
    expect(r.code).toBe("SH600519");
    expect(r.price).toBe(1500.12);          // 2 位小数
  });
  it("direction 非法 → 抛错", () => {
    expect(() => normalizeOrderPayload({ code: "600519.SH", direction: "hold", volume: 100, price: 10 })).toThrow("方向须为 buy / sell");
  });
  it("volume 整数化（手），非正数 → 抛错", () => {
    const r = normalizeOrderPayload({ code: "600519.SH", direction: "sell", volume: 100.7, price: 10 });
    expect(r.volume).toBe(100);
    expect(() => normalizeOrderPayload({ code: "600519.SH", volume: 0, price: 10 })).toThrow("数量必须为正整数");
    expect(() => normalizeOrderPayload({ code: "600519.SH", volume: -5, price: 10 })).toThrow("数量必须为正整数");
  });
  it("price 非法 → 抛错；price_type 白名单", () => {
    expect(() => normalizeOrderPayload({ code: "600519.SH", volume: 100, price: 0 })).toThrow("价格必须大于 0");
    expect(() => normalizeOrderPayload({ code: "600519.SH", volume: 100, price: 10, price_type: "iceberg" })).toThrow("price_type 仅支持");
  });
  it("默认值：direction=buy / price_type=limit / strategy_name=manual", () => {
    const r = normalizeOrderPayload({ code: "000001.SZ", volume: 200, price: 5 });
    expect(r.direction).toBe("buy");
    expect(r.price_type).toBe("limit");
    expect(r.strategy_name).toBe("manual");
  });
});

describe("explainTradeError（错误业务化映射）", () => {
  it("未连接券商 → 引导", () => {
    expect(explainTradeError("bridge handshake timeout: None")).toBe("未连接券商：请到「券商连接」添加并连接券商");
  });
  it("风控拒绝 → 精简", () => {
    expect(explainTradeError("风控拒绝：单笔金额超限")).toBe("风控拦截：单笔金额超限");
  });
  it("资金/持仓/权限/价格越界分类", () => {
    expect(explainTradeError("可用资金不足")).toContain("可用资金不足");
    expect(explainTradeError("可卖持仓不足")).toContain("可卖持仓不足");
    expect(explainTradeError("权限拒绝")).toContain("券商拒绝");
    expect(explainTradeError("价格超出涨停价")).toContain("价格越界");
  });
  it("未知错误 → 原文兜底", () => {
    expect(explainTradeError("custom unknown err")).toBe("custom unknown err");
    expect(explainTradeError("")).toBe("委托失败");
  });
});

describe("precheckOrder / sendOrder（后端调用 + 错误归一）", () => {
  it("precheck 通过", async () => {
    api.post = async () => ({ allowed: true, risk: { score: 5 } });
    const r = await precheckOrder({ code: "600519.SH", volume: 100, price: 1500 });
    expect(r.ok).toBe(true);
    expect(r.allowed).toBe(true);
  });
  it("precheck 业务拒绝 → explainTradeError 映射", async () => {
    api.post = async () => ({ allowed: false, reason: "风控拒绝：日交易次数超限" });
    const r = await precheckOrder({ code: "600519.SH", volume: 100, price: 1500 });
    expect(r.ok).toBe(true);
    expect(r.allowed).toBe(false);
    expect(r.reason).toBe("风控拦截：日交易次数超限");
  });
  it("precheck 网络错误 → ok=false + 友好文案", async () => {
    api.post = async () => { throw new Error("bridge handshake timeout: None"); };
    const r = await precheckOrder({ code: "600519.SH", volume: 100, price: 1500 });
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("未连接券商");
  });
  it("sendOrder 字段非法 → 抛出（含 cause）", async () => {
    await expect(sendOrder({ code: "", volume: 100, price: 10 })).rejects.toThrow();
  });
  it("sendOrder 未传幂等键时不自生成（交给后端滚动窗口内容哈希去重）", async () => {
    let captured;
    api.tradeOrder = async (b) => { captured = b; return { id: "x" }; };
    await sendOrder({ code: "600519.SH", volume: 100, price: 1500 });
    // 2026-09 前端移除「内容+5s 时间桶」自生成键：桶边界会绕过后端
    // single_flight 的滚动窗口去重，故仅透传显式幂等键
    expect(captured.idempotency_key || '').toBe('');  // 归一层可能置空串
  });

  it("sendOrder 显式幂等键原样透传", async () => {
    let captured;
    api.tradeOrder = async (b) => { captured = b; return { id: "x" }; };
    await sendOrder({ code: "600519.SH", volume: 100, price: 1500, idempotency_key: "explicit-key" });
    expect(captured.idempotency_key).toBe("explicit-key");
  });
});
