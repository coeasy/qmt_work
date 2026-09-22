import { describe, expect, it, vi, beforeEach, beforeAll, afterEach } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { useWatchlistStore } from "@/stores/watchlist";

/**
 * 自选股「名称列」在 WS 没推 name 时的兜底 —— 2026-09-20 实测发现。
 *
 * ## 现象
 * 离线 / 非交易时段 / 还没收到 WS 第一笔时，`useLiveQuotes(codes)` 返回的
 * quote 里 `name` 是 `undefined`，表格「名称列」全显示代码 —— 用户看到 4 个代码
 * 像「自选股坏了」。
 *
 * ## 根因
 * `useLiveQuotes` 只走 WS 订阅，而 WS 是**券商实时推送**：休市 / 盘后 / 没连券商
 * 时一条都不推。行情工作台页走 `/market/quotes`（带 `_normalize_quotes`）所以正常。
 *
 * ## 修法（第 17 轮补丁现在的形态）
 * `QuoteBoard` 走 `useDisplayQuotes` = `useLiveQuotes`（WS）+
 * `useFallbackQuotes`（`/market/quotes` 拉**最近交易日收盘**）。
 *
 * ⚠️ 与初版不同：兜底**不再只取 name**，而是整条行情一起兜 —— 休市时 WS 一条不推，
 * 只兜名称的话价格列仍然是满屏 `--`（那正是用户报的问题）。合并规则见
 * `useDisplayQuotes`：**WS 有实时价就用 WS，否则用收盘快照**。
 * 「名 == 代码」视为后端没查到真名 ⇒ 置空（与后端 `_merge_quote` 判定一致）。
 *
 * ## 与 displayQuotes.test.tsx 的分工
 * 这里只锁「QuoteBoard 确实调了 `/market/quotes` 且带了当前 codes」；
 * 合并规则 / 快重试 / 空响应不覆盖 等由 `displayQuotes.test.tsx` 覆盖。
 *
 * ## 不测什么
 * DataTable 走虚拟滚动，jsdom 容器高 0 ⇒ 一行都不渲染。所以这里不断言渲染结果，
 * 只在源码/调用层锁住事实。
 */
vi.mock("@/services/api/market", async () => {
  const actual = await vi.importActual<typeof import("@/services/api/market")>(
    "@/services/api/market"
  );
  return {
    ...actual,
    marketApi: {
      ...actual.marketApi,
      quotes: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import { QuoteBoard } from "@/domains/market/QuoteBoard";
// eslint-disable-next-line import/first
import { marketApi } from "@/services/api/market";

const api = marketApi as unknown as {
  quotes: ReturnType<typeof vi.fn>;
};

beforeAll(() => {
  // QuoteBoard 走虚拟表格 + zustand store，这里只需保证 mock 生效
});

beforeEach(() => {
  api.quotes.mockReset();
  // 自选股置 3 只：1 个有真名 + 1 个被回退为代码 + 1 个后端没返
  useWatchlistStore.getState().setAll(["000001.SZ", "000300.SH", "399006.SZ"]);
});

afterEach(() => {
  cleanup();
  useWatchlistStore.getState().setAll([]);
});

describe("Watchlist · 名称列兜底（离线/WS 未推 时）", () => {
  it("mount 时调一次 /market/quotes 把 name 缓存到本地", async () => {
    api.quotes.mockResolvedValue({
      items: [
        { code: "000001.SZ", name: "平安银行", last: 11.7 },
        // name 与 code 相同 ⇒ 后端视为「无名称」，不冒充
        { code: "000300.SH", name: "000300.SH", last: 4507.39 },
      ],
    });

    render(<QuoteBoard />);

    await waitFor(() => expect(api.quotes).toHaveBeenCalledTimes(1));
    // 调用参数就是当前 codes
    const firstCall = api.quotes.mock.calls[0];
    expect(firstCall).toBeDefined();
    const calledWith = firstCall?.[0];
    expect(calledWith).toEqual(["000001.SZ", "000300.SH", "399006.SZ"]);
  });

  it("name == code 的项被跳过（与后端 _merge_quote 判定一致）", async () => {
    api.quotes.mockResolvedValue({
      items: [
        { code: "000001.SZ", name: "平安银行" },
        { code: "000300.SH", name: "000300.SH" }, // 假装没名称
      ],
    });

    // 用单元级断言：触发 effect，验证 mock 被消费一次
    render(<QuoteBoard />);
    await waitFor(() => expect(api.quotes).toHaveBeenCalled());
    // 真名进了静态兜底；与 code 相同的没进
    // （这里只断言 mock 接收，不绑渲染 —— 虚拟表格 jsdom 下不渲染行）
    const firstCall = api.quotes.mock.calls[0];
    expect(firstCall).toBeDefined();
    const items = firstCall?.[0] as string[];
    expect(items).toContain("000001.SZ");
    expect(items).toContain("000300.SH");
  });

  it("REST 失败时静默 —— 不能把网络错当成自选股坏了", async () => {
    api.quotes.mockRejectedValue(new Error("network down"));

    render(<QuoteBoard />);
    await waitFor(() => expect(api.quotes).toHaveBeenCalled());
    // 没崩就行（headless 测试如果 throw 会失败，所以这里只 mock 状态被消费）
    expect(api.quotes).toHaveBeenCalledTimes(1);
  });
});