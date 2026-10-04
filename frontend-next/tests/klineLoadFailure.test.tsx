import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { cleanup, render, waitFor, act } from "@testing-library/react";

/**
 * K 线图 overlay 的**诚实性**回归测试（R23c）。
 *
 * ## 这条缺陷为什么危险
 * `getBars` 的失败分支此前与「真的没数据」共用 `setEmpty(true)` 一条路：
 *
 * ```ts
 * .catch(() => { setEmpty(true); callback([], false); });   // ← 吞掉原因
 * ```
 *
 * 于是 overlay 会渲染成 `暂无 K 线数据（可能停牌或该周期无成交）` ——
 * **确定性断言 + 成因指错**：请求失败（503 / 断网 / 后端异常）被读成「标的停牌」。
 * 更隐蔽的是失败分支**没有清 `meta`**：上一轮成功时的 `note` / `source` / `asOf`
 * 会留下来，overlay 用**旧的 note** 解释**这一次的失败**，头部继续显示旧的
 * 「数据源 / 数据截至」——看起来一切正常。
 *
 * ## 三条锁（都可证伪）
 *  1. 失败必须说「加载失败」并带上真实原因，且**不得**出现「停牌」；
 *  2. 失败后不得沿用上一轮成功时的 `note` / `source`（陈旧元数据必须清掉）；
 *  3. 真的返回空 bars（成功）**不得**被误报成「加载失败」——防止修过头。
 *
 * 第 3 条是防误伤：只测「失败要报错」的话，把所有空态都改成「失败」也能过。
 */

/** 捕获 setDataLoader 交进来的 getBars（避免 vi.hoisted 的 TDZ 问题，挂在 globalThis 上） */
type GetBarsArg = {
  type: string;
  timestamp?: number;
  callback: (data: unknown[], more: boolean) => void;
};

vi.mock("klinecharts", () => ({
  init: () => ({
    setSymbol: () => {},
    setPeriod: () => {},
    setDataLoader: (l: { getBars: (p: GetBarsArg) => void }) => {
      (globalThis as unknown as { __klineLoader: unknown }).__klineLoader = l.getBars;
    },
    // ★ 记录调用次数：主题 / 涨跌配色变化后必须**重新 apply 样式**（见下方第 4 条锁）。
    //   此前 chartStyles() 的注释写着「主题切换时重建」，代码里却没有这条路径 ——
    //   注释描述了一个不存在的机制。这个桩让「切了配色却没重刷」变成红灯。
    setStyles: (styles: unknown) => {
      const g = globalThis as unknown as {
        __klineStyleCalls?: number;
        __klineStyles?: unknown;
      };
      g.__klineStyleCalls = (g.__klineStyleCalls ?? 0) + 1;
      g.__klineStyles = styles;
    },
    resize: () => {},
    createIndicator: () => {},
    removeIndicator: () => {},
    getIndicators: () => [],
    subscribeAction: () => {},
    unsubscribeAction: () => {},
    scrollToDataIndex: () => {},
  }),
  dispose: () => {},
}));

vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    marketApi: { ...actual.marketApi, kline: vi.fn() },
  };
});

// eslint-disable-next-line import/first
import { marketApi } from "@/services/api";
// eslint-disable-next-line import/first
import { KLineChart } from "@/charts/KLineChart";
// eslint-disable-next-line import/first
import { useUiStore } from "@/stores/ui";

const mockKline = marketApi.kline as unknown as ReturnType<typeof vi.fn>;

function getLoader(): (p: GetBarsArg) => void {
  const fn = (globalThis as unknown as { __klineLoader?: unknown }).__klineLoader;
  if (typeof fn !== "function") throw new Error("getBars 未被 setDataLoader 捕获");
  return fn as (p: GetBarsArg) => void;
}

/** 渲染一次并触发一轮 getBars */
async function loadOnce(): Promise<void> {
  await act(async () => {
    getLoader()({ type: "init", timestamp: 0, callback: () => {} });
    await Promise.resolve();
  });
}

const ONE_BAR = {
  time: "20260817",
  open: 10,
  high: 11,
  low: 9.5,
  close: 10.5,
  volume: 1000,
};

describe("KLineChart 空态/失败态诚实性", () => {
  beforeEach(() => {
    (globalThis as unknown as { __klineLoader?: unknown }).__klineLoader = undefined;
    mockKline.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("请求失败必须说「加载失败」并给出真实原因，不得断言「停牌」", async () => {
    mockKline.mockRejectedValue(new Error("HTTP 503 数据源不可用"));
    render(<KLineChart code="600000.SH" period="1d" />);

    await loadOnce();

    await waitFor(() => {
      const txt = document.body.textContent ?? "";
      expect(txt).toMatch(/K 线加载失败/);
    });
    const txt = document.body.textContent ?? "";
    // 真实原因必须透出来，不能被糊成一句通用文案
    expect(txt).toContain("HTTP 503 数据源不可用");
    // ★ 否定式断言：失败绝不能被读成「停牌」
    expect(txt).not.toMatch(/停牌/);
    expect(txt).not.toMatch(/该周期无成交/);
  });

  it("失败后不得沿用上一轮成功时的 note / source（陈旧元数据必须清掉）", async () => {
    mockKline.mockResolvedValueOnce({
      bars: [ONE_BAR],
      source: "eltdx",
      note: "源降级：使用本地缓存",
    });
    render(<KLineChart code="600000.SH" period="1d" />);
    await loadOnce();

    // 先确认上一轮的元数据确实渲染出来了（否则本用例是空转的）。
    // 注意：note 只落在 meta 的 `title` 属性上，不在 textContent 里。
    await waitFor(() => {
      expect(document.body.textContent ?? "").toContain("eltdx");
    });
    const titlesBefore = Array.from(document.querySelectorAll("[title]")).map(
      (el) => el.getAttribute("title") ?? "",
    );
    expect(titlesBefore.some((t) => t.includes("源降级：使用本地缓存"))).toBe(true);

    // 再让下一次请求失败
    mockKline.mockRejectedValue(new Error("Network Error"));
    await loadOnce();

    await waitFor(() => {
      expect(document.body.textContent ?? "").toMatch(/K 线加载失败/);
    });
    const txt = document.body.textContent ?? "";
    // ★ 旧 note / 旧 source 都不能再出现（否则是「用旧原因解释新失败」）
    const titlesAfter = Array.from(document.querySelectorAll("[title]")).map(
      (el) => el.getAttribute("title") ?? "",
    );
    expect(titlesAfter.some((t) => t.includes("源降级：使用本地缓存"))).toBe(false);
    expect(txt).not.toContain("eltdx");
    expect(txt).toContain("Network Error");
  });

  it("成功但确实没有 K 线时，不得误报成「加载失败」（防修过头）", async () => {
    mockKline.mockResolvedValue({ bars: [] });
    render(<KLineChart code="600000.SH" period="1d" />);

    await loadOnce();

    await waitFor(() => {
      expect(document.body.textContent ?? "").toMatch(/暂无 K 线数据/);
    });
    const txt = document.body.textContent ?? "";
    expect(txt).not.toMatch(/K 线加载失败/);
  });

  /**
   * 第 4 条锁（2026-10-03）：**主题 / 涨跌配色变化必须重刷图表样式**。
   *
   * `chartStyles()` 的注释此前写着「主题切换时重建」，但 `init()` 只在建图时取一次
   * 令牌，代码里**没有**任何一条随主题变化的重刷路径 —— 注释描述了不存在的机制。
   * 后果：切到浅色或换涨跌口径后，画布上的蜡烛与成交量柱仍是建图那一刻的颜色，
   * 只有切走页签再切回（图表销毁重建）才生效。
   *
   * 判据可证伪：把 KLineChart 里那个 `setStyles` effect 删掉，本用例必红。
   */
  it("切换涨跌配色 / 明暗主题后必须重新 apply 样式（否则画布停留在建图时的颜色）", async () => {
    mockKline.mockResolvedValue({ bars: [ONE_BAR] });
    render(<KLineChart code="600000.SH" period="1d" />);
    await loadOnce();

    const calls = () =>
      (globalThis as unknown as { __klineStyleCalls?: number }).__klineStyleCalls ?? 0;
    const before = calls();

    await act(async () => {
      useUiStore.getState().setUpdown("green-up");
      await Promise.resolve();
    });
    expect(calls(), "切了涨跌配色却没有重刷图表样式").toBeGreaterThan(before);

    const mid = calls();
    await act(async () => {
      useUiStore.getState().setTheme("light");
      await Promise.resolve();
    });
    expect(calls(), "切了明暗主题却没有重刷图表样式").toBeGreaterThan(mid);
  });

  it("成功返回数据后 overlay 必须消失（失败态不得残留）", async () => {
    mockKline.mockRejectedValueOnce(new Error("HTTP 500"));
    render(<KLineChart code="600000.SH" period="1d" />);
    await loadOnce();
    await waitFor(() => {
      expect(document.body.textContent ?? "").toMatch(/K 线加载失败/);
    });

    mockKline.mockResolvedValue({ bars: [ONE_BAR] });
    await loadOnce();

    await waitFor(() => {
      const txt = document.body.textContent ?? "";
      expect(txt).not.toMatch(/K 线加载失败/);
      expect(txt).not.toMatch(/暂无 K 线数据/);
    });
  });
});
