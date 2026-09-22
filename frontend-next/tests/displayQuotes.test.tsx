import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";

/**
 * 看板类列表的行情兜底 —— 2026-09-20 第 17 轮补丁。
 *
 * ## 要锁的四个事实
 *
 * ① **双源合并的优先级**：WS 有实时价就用 WS，没有才用最近交易日收盘快照，
 *    两边都没有才是 `undefined`（渲染成 `--`）。顺序反了会出现「开市了却显示
 *    昨收」或「休市了却显示 0」两种假象。
 *
 * ② **判据必须是 `isLivePrice`**：`price: 0` 是「停牌/没订阅到」而不是「跌到 0」。
 *    用 `!= null` 判断会让 0 冒充收盘价 —— 这是本项目反复强调的「零 mock」铁律。
 *
 * ③ **空响应不覆盖已有数据**：后端公共源有全局节流，冷启动首次经常拿不到东西。
 *    若空响应把已展示的数字清掉，界面会「有 → 空 → 有」闪一下，比一直空更糟。
 *
 * ④ **看板类列表不得直接用 `useLiveQuotes`**：休市时满屏 `--`。用源码扫描锁住，
 *    防止以后有人图省事改回去（这类回归在单测里看不出来，只有休市那天才发现）。
 */
vi.mock("@/services/api/market", async () => {
  const actual = await vi.importActual<typeof import("@/services/api/market")>(
    "@/services/api/market",
  );
  return {
    ...actual,
    marketApi: { ...actual.marketApi, quotes: vi.fn() },
  };
});

// WS 订阅在 jsdom 下无意义（且会试图连 WebSocket），这里桩掉 —— 只测合并规则
vi.mock("@/hooks/useQuoteSubscription", () => ({
  useQuoteSubscription: () => undefined,
}));

// eslint-disable-next-line import/first
import { marketApi } from "@/services/api/market";
// eslint-disable-next-line import/first
import { useQuotesStore } from "@/stores/quotes";
// eslint-disable-next-line import/first
import { useDisplayQuotes, useFallbackQuotes } from "@/hooks/useLiveQuotes";
// eslint-disable-next-line import/first
import type { Quote } from "@/shared/types";

const api = marketApi as unknown as { quotes: ReturnType<typeof vi.fn> };

const CODES = ["000001.SZ", "600519.SH"];

/** 造一条行情（只填测试关心的字段） */
const q = (code: string, price: number | undefined, name = ""): Quote =>
  ({ code, name, price }) as unknown as Quote;

function seedLive(quotes: Record<string, Quote>) {
  useQuotesStore.setState({ quotes });
}

beforeEach(() => {
  api.quotes.mockReset();
  api.quotes.mockResolvedValue({ items: [] });
  seedLive({});
});

afterEach(() => {
  seedLive({});
});

describe("useDisplayQuotes · 双源合并", () => {
  it("WS 有实时价 ⇒ 用 WS 的价（不是昨收）", async () => {
    api.quotes.mockResolvedValue({
      items: [{ code: "000001.SZ", name: "平安银行", price: 11.7 }],
    });
    seedLive({ "000001.SZ": q("000001.SZ", 12.0) });

    const { result } = renderHook(() => useDisplayQuotes(CODES));
    await waitFor(() => expect(result.current["000001.SZ"]).toBeDefined());

    expect(result.current["000001.SZ"]?.price).toBe(12.0);
  });

  it("WS 价为 0 ⇒ 视为没行情，回退收盘快照（0 不冒充）", async () => {
    api.quotes.mockResolvedValue({
      items: [{ code: "000001.SZ", name: "平安银行", price: 11.7 }],
    });
    // 停牌 / 未订阅到时 WS 会推 price: 0
    seedLive({ "000001.SZ": q("000001.SZ", 0) });

    const { result } = renderHook(() => useDisplayQuotes(CODES));
    await waitFor(() => expect(result.current["000001.SZ"]?.price).toBe(11.7));
  });

  it("两边都没有 ⇒ undefined（渲染成 --，不是 0）", () => {
    api.quotes.mockResolvedValue({ items: [] });
    const { result } = renderHook(() => useDisplayQuotes(CODES));
    expect(result.current["000001.SZ"]).toBeUndefined();
  });

  it("WS 有价但没名 ⇒ 用兜底名补齐（不留空名称列）", async () => {
    api.quotes.mockResolvedValue({
      items: [{ code: "000001.SZ", name: "平安银行", price: 11.7 }],
    });
    // 券商 WS 快照经常不带中文名
    seedLive({ "000001.SZ": q("000001.SZ", 12.0, "") });

    const { result } = renderHook(() => useDisplayQuotes(CODES));
    await waitFor(() => expect(result.current["000001.SZ"]?.name).toBe("平安银行"));
    // 价仍是 WS 的
    expect(result.current["000001.SZ"]?.price).toBe(12.0);
  });
});

describe("useFallbackQuotes · 兜底快照", () => {
  it("首次空响应 ⇒ 1s 后快重试（不干等 15s 轮询）", async () => {
    api.quotes
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({
        items: [{ code: "000001.SZ", name: "平安银行", price: 11.7 }],
      });

    const { result } = renderHook(() => useFallbackQuotes(["000001.SZ"]));
    // 快重试窗口 1s，给 4s 余量（CI 慢机器）
    await waitFor(() => expect(api.quotes).toHaveBeenCalledTimes(2), { timeout: 4000 });
    await waitFor(() => expect(result.current["000001.SZ"]?.price).toBe(11.7));
  });

  it("空响应不覆盖已展示的数据（避免「有 → 空」闪烁）", async () => {
    api.quotes.mockResolvedValueOnce({
      items: [{ code: "000001.SZ", name: "平安银行", price: 11.7 }],
    });
    const { result, rerender } = renderHook(
      ({ codes }: { codes: string[] }) => useFallbackQuotes(codes),
      { initialProps: { codes: ["000001.SZ"] } },
    );
    await waitFor(() => expect(result.current["000001.SZ"]?.price).toBe(11.7));

    // 之后后端开始返空（节流/超时都可能导致），key 变化触发重拉
    api.quotes.mockResolvedValue({ items: [] });
    rerender({ codes: ["000001.SZ", "600519.SH"] });
    await waitFor(() => expect(api.quotes).toHaveBeenCalledTimes(2));

    expect(result.current["000001.SZ"]?.price).toBe(11.7);
  });

  it("「名 == 代码」视为无名称 ⇒ 置空，不拿代码冒充", async () => {
    api.quotes.mockResolvedValue({
      items: [{ code: "000300.SH", name: "000300.SH", price: 4507.39 }],
    });

    const { result } = renderHook(() => useFallbackQuotes(["000300.SH"]));
    await waitFor(() => expect(result.current["000300.SH"]).toBeDefined());
    expect(result.current["000300.SH"]?.name).toBe("");
    // 价格照常保留 —— 只是名字不冒充
    expect(result.current["000300.SH"]?.price).toBe(4507.39);
  });

  it("REST 失败时静默 —— 不能把网络错当成「自选股坏了」", async () => {
    api.quotes.mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useFallbackQuotes(CODES));
    await waitFor(() => expect(api.quotes).toHaveBeenCalled());
    expect(result.current["000001.SZ"]).toBeUndefined();
  });

  it("codes 为空 ⇒ 一个请求都不发", () => {
    const { result } = renderHook(() => useFallbackQuotes([]));
    expect(api.quotes).not.toHaveBeenCalled();
    expect(result.current).toEqual({});
  });
});

describe("看板类列表不得直接用 useLiveQuotes", () => {
  const SRC = path.resolve(__dirname, "../src");

  it("QuoteBoard / DataPanel 必须走 useDisplayQuotes", () => {
    for (const rel of ["domains/market/QuoteBoard.tsx", "shell/DataPanel.tsx"]) {
      const src = fs.readFileSync(path.join(SRC, rel), "utf8");
      expect(src, `${rel} 未使用 useDisplayQuotes（休市会满屏 --）`).toContain(
        "useDisplayQuotes",
      );
      // 直接订阅 WS 的那一个不许再出现在这两个文件里
      const body = src.replace(/^import[\s\S]*?from\s+"[^"]+";\s*$/gm, "");
      expect(body, `${rel} 仍在直接用 useLiveQuotes`).not.toContain("useLiveQuotes(");
    }
  });
});
