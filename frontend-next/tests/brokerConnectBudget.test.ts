import { describe, expect, it, afterEach, vi } from "vitest";

/**
 * 券商「长耗时端点」的**超时预算**回归测试。
 *
 * 为什么必须钉死：
 * 后端一次 `POST /brokers/{id}/connect` 最坏要跑
 * 「桥接子进程冷启动 + 候选数据目录(≤2) × session(6) 逐个 XtQuantTrader.connect()」，
 * 实测约 45s（`XTPQuantAdapter._CONNECT_RETRY_BUDGET`），加子进程启动余量接近 60s。
 * 而 `http.ts` 的默认超时是 **15s**（`DEFAULT_TIMEOUT`）。
 * 若 connect / test 忘记覆盖超时，请求会在**诊断信息产出之前**被 abort，
 * 用户只看到「请求超时（15s）」—— 后端辛苦构造的根因（客户端日志证据 +
 * 官方四步排查）永远到不了界面。
 *
 * 这是一条**真实发生过的「前后端断链」**（R2 修复），且极易在后人重构
 * `http.post(path, body)` 时被静默回退，故用假时钟把预算钉住。
 */

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/**
 * 造一个「永不 resolve」的 fetch，只记录 signal 被 abort 的时刻。
 * 这样就能在不真正等待的前提下观察**超时阈值到底是多少**。
 */
function neverResolvingFetch(seen: { abortedAtMs: number | null }) {
  const fn = vi.fn((_url: string, init?: { signal?: AbortSignal }) => {
    return new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        seen.abortedAtMs = Date.now();
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      });
    });
  });
  vi.stubGlobal("fetch", fn as never);
  return fn;
}

describe("券商长耗时端点 · 超时预算", () => {
  it("connect 不得落到 15s 默认超时（否则后端诊断永远到不了前端）", async () => {
    vi.useFakeTimers();
    const seen = { abortedAtMs: null as number | null };
    neverResolvingFetch(seen);
    const { brokerApi } = await import("@/services/api/broker");

    const pending = brokerApi.connect("conn-1").catch((e: Error) => e.message);

    // 跨过默认的 15s：此时**绝不允许** abort
    await vi.advanceTimersByTimeAsync(16_000);
    expect(
      seen.abortedAtMs,
      "connect 在 16s 就被 abort ⇒ 命中了 15s 默认超时，后端诊断会全部丢失",
    ).toBeNull();

    // 后端最坏 ~45s + 子进程冷启动：60s 处仍不应超时
    await vi.advanceTimersByTimeAsync(44_000);
    expect(
      seen.abortedAtMs,
      "connect 在 60s 就被 abort，低于后端最坏耗时（约 45s 重试预算 + 子进程启动）",
    ).toBeNull();

    // 走到显式上限（120s）才允许超时，且文案须给出真实秒数便于排查
    await vi.advanceTimersByTimeAsync(61_000);
    const msg = await pending;
    console.log("   connect 超时文案 =", msg);
    expect(msg).toMatch(/请求超时/);
    expect(msg).toMatch(/120s/);
  });

  it("/brokers/test 与 connect 同源，超时同样不得低于 60s", async () => {
    vi.useFakeTimers();
    const seen = { abortedAtMs: null as number | null };
    neverResolvingFetch(seen);
    const { brokerApi } = await import("@/services/api/broker");

    const pending = brokerApi
      .test({ broker_id: "generic", client_path: "P:/stock/gd_qmt" })
      .catch((e: Error) => e.message);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(
      seen.abortedAtMs,
      "/brokers/test 在 60s 内就被 abort —— 探测同样要跑适配器 start()，必须给足预算",
    ).toBeNull();

    await vi.advanceTimersByTimeAsync(61_000);
    const msg = await pending;
    console.log("   test 超时文案 =", msg);
    expect(msg).toMatch(/120s/);
  });

  it("auto-detect 全盘扫描需 >15s，且不得把 25s 级扫描重试两遍", async () => {
    vi.useFakeTimers();
    const seen = { abortedAtMs: null as number | null };
    const fn = neverResolvingFetch(seen);
    const { brokerApi } = await import("@/services/api/broker");

    const pending = brokerApi.autoDetect().catch((e: Error) => e.message);

    await vi.advanceTimersByTimeAsync(30_000);
    expect(
      seen.abortedAtMs,
      "auto-detect 在 30s 内被 abort —— 实测全盘扫描约 25s，默认 15s 会把「正在发现」误报成「发现失败」",
    ).toBeNull();
    expect(fn, "auto-detect 是重操作，重试会把 25s 级扫描白跑两遍").toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(31_000);
    const msg = await pending;
    console.log("   auto-detect 超时文案 =", msg);
    expect(msg).toMatch(/60s/);
  });
});
