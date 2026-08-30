// G4 数据面 topic 总线测试：策略驱动缓存/节流/合并/陈旧快照。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetForTests, invalidate, loadPolicies, peek, policyOf, subscribe } from "../dataHub.js";

vi.mock("../../api.js", () => ({
  api: { dataHubPolicies: vi.fn() },
}));
import { api } from "../../api.js";

beforeEach(() => {
  __resetForTests();
  vi.useFakeTimers();
  api.dataHubPolicies.mockResolvedValue({
    default: { ttl_ms: 30000, min_interval_ms: 10000, coalesce_within_ms: 2000,
               priority: "medium", stale_ok: true },
    topics: {
      "market:boards": { ttl_ms: 10000, min_interval_ms: 5000, coalesce_within_ms: 1000,
                         priority: "medium", stale_ok: true },
    },
  });
});
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("policyOf", () => {
  it("最长前缀匹配", async () => {
    await loadPolicies();
    expect(policyOf("market:boards:industry").ttl_ms).toBe(10000);
    expect(policyOf("market:quote:600519.SH").ttl_ms).toBe(30000);  // 回退默认
  });
});

describe("subscribe", () => {
  it("首次订阅发起请求并回调数据", async () => {
    await loadPolicies();
    const fetcher = vi.fn().mockResolvedValue([1, 2, 3]);
    const cb = vi.fn();
    subscribe("market:boards:industry", fetcher, cb);
    await vi.advanceTimersByTimeAsync(1100);          // 越过 coalesce 窗口
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(cb).toHaveBeenCalledWith({ data: [1, 2, 3], stale: false });
  });

  it("ttl 内重复订阅直接命中快照（零网络）", async () => {
    await loadPolicies();
    const fetcher = vi.fn().mockResolvedValue([9]);
    const cb1 = vi.fn();
    subscribe("market:boards:industry", fetcher, cb1);
    await vi.advanceTimersByTimeAsync(1100);
    const cb2 = vi.fn();
    subscribe("market:boards:industry", fetcher, cb2);
    expect(fetcher).toHaveBeenCalledTimes(1);         // 未再发请求
    expect(cb2).toHaveBeenCalledWith({ data: [9], stale: false });
  });

  it("coalesce 窗口内多个订阅合并为一次请求", async () => {
    await loadPolicies();
    const fetcher = vi.fn().mockResolvedValue([1]);
    const cbs = [vi.fn(), vi.fn(), vi.fn()];
    cbs.forEach((cb) => subscribe("market:boards:industry", fetcher, cb));
    await vi.advanceTimersByTimeAsync(1100);
    expect(fetcher).toHaveBeenCalledTimes(1);
    cbs.forEach((cb) => expect(cb).toHaveBeenCalledTimes(1));
  });

  it("min_interval 内重复订阅返回陈旧快照（stale:true）", async () => {
    // 定制策略：ttl=0（永不过期成新鲜）+ min_interval 大 → 二次订阅走陈旧路径
    api.dataHubPolicies.mockResolvedValue({
      default: { ttl_ms: 0, min_interval_ms: 5000, coalesce_within_ms: 1000,
                 priority: "medium", stale_ok: true }, topics: {},
    });
    await loadPolicies();
    const cb1 = vi.fn();
    subscribe("market:boards:industry", vi.fn().mockResolvedValue([1]), cb1);
    await vi.advanceTimersByTimeAsync(1100);              // 首次请求完成
    expect(cb1).toHaveBeenCalledWith({ data: [1], stale: false });
    const fetcher2 = vi.fn();
    const cb2 = vi.fn();
    subscribe("market:boards:industry", fetcher2, cb2);   // 立即再订阅
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher2).not.toHaveBeenCalled();              // 未发新请求
    expect(cb2).toHaveBeenCalledWith({ data: [1], stale: true });
  });

  it("fetcher 失败回调 error 且不写快照", async () => {
    await loadPolicies();
    const fetcher = vi.fn().mockRejectedValue(new Error("网络断开"));
    const cb = vi.fn();
    subscribe("market:boards:industry", fetcher, cb);
    await vi.advanceTimersByTimeAsync(1100);
    expect(cb).toHaveBeenCalledWith(expect.objectContaining({ data: null, stale: true }));
    expect(cb.mock.calls[0][0].error.message).toBe("网络断开");
    expect(peek("market:boards:industry")).toBeNull();
  });
});

describe("peek/invalidate", () => {
  it("peek 读快照，invalidate 清空", async () => {
    await loadPolicies();
    subscribe("market:boards:industry", vi.fn().mockResolvedValue([7]), vi.fn());
    await vi.advanceTimersByTimeAsync(1100);
    expect(peek("market:boards:industry").data).toEqual([7]);
    invalidate("market:boards:industry");
    expect(peek("market:boards:industry")).toBeNull();
  });
});
