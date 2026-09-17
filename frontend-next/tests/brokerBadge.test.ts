import { describe, expect, it } from "vitest";
import { brokerBadge } from "@/stores/broker";

/**
 * 状态栏「券商」角标回归测试。
 *
 * 锁的是**非正常态必须给出路**这条产品约束：
 * 旧实现里未连券商只显示 `券商 0/0` 且不可点击，用户既不知道原因也不知道该做什么。
 * 这里把「每档带哪个动作」钉死，防止以后有人为了省事把引导删成一句静态文案。
 */

const conn = (connected: boolean) => ({ connected });

describe("brokerBadge · 每档都有明确出路", () => {
  it("读取中：显示进度，不可点击（此时本来就没得点）", () => {
    const b = brokerBadge([], true, "");
    expect(b.label).toBe("券商 读取中…");
    expect(b.tone).toBe("idle");
    expect(b.action).toBeNull();
  });

  it("读取失败：可点击重试，且 tooltip 带上失败原因", () => {
    const b = brokerBadge([], false, "connect ECONNREFUSED");
    expect(b.label).toBe("券商 状态未知");
    expect(b.tone).toBe("err");
    expect(b.action).toBe("retry");
    expect(b.title).toContain("ECONNREFUSED");
  });

  it("★ 未配置任何连接：引导去「连接管理」建连（而不是只显示 0/0）", () => {
    const b = brokerBadge([], false, "");
    expect(b.label).toBe("未连接券商");
    expect(b.action).toBe("manage");
    expect(b.title).toContain("自动识别本机 QMT 客户端");
  });

  it("★ 已配置但全未连上：引导去排障/重连（与「未配置」区分开）", () => {
    const b = brokerBadge([conn(false), conn(false)], false, "");
    expect(b.label).toBe("券商 0/2");
    expect(b.action).toBe("manage");
    expect(b.title).toContain("查看原因并重连");
  });

  it("★ 「未配置」与「配了没连上」必须是两种文案（否则用户会去错地方）", () => {
    const none = brokerBadge([], false, "");
    const failed = brokerBadge([conn(false)], false, "");
    expect(none.title).not.toBe(failed.title);
    expect(none.label).not.toBe(failed.label);
  });

  it("部分连上：显示比例且仍可进管理页", () => {
    const b = brokerBadge([conn(true), conn(false), conn(true)], false, "");
    expect(b.label).toBe("券商 2/3");
    expect(b.tone).toBe("ok");
    expect(b.action).toBe("manage");
  });

  it("全部连上：正常态", () => {
    const b = brokerBadge([conn(true)], false, "");
    expect(b.label).toBe("券商 1/1");
    expect(b.tone).toBe("ok");
  });

  it("loading 与 error 同时存在时，error 优先（不能让「读取中」盖住失败）", () => {
    const b = brokerBadge([], true, "boom");
    expect(b.label).toBe("券商 状态未知");
    expect(b.action).toBe("retry");
  });
});
