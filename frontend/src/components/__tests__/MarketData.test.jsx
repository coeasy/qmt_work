// @vitest-environment jsdom
// MarketData（行情分析页）拆分后编排层冒烟：渲染契约 / 周期切换 / 订阅回写 / 抽屉面板切换。
// 重依赖（Chart=echarts、QuotePanel、行情 hooks、api）全部 mock —— 只验证编排与交互契约。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../Chart.jsx", () => ({ default: () => <div data-testid="chart" /> }));
vi.mock("../QuotePanel.jsx", () => ({ default: () => <div data-testid="quote-panel" /> }));
vi.mock("../ErrorBoundary.jsx", () => ({ default: ({ children }) => <div>{children}</div> }));

vi.mock("../../hooks/useMarket.js", () => ({
  useKline: vi.fn(() => ({
    data: { bars: [{ time: "2026-09-01", open: 10, close: 11, high: 12, low: 9, volume: 100 }], source: "eltdx" },
    loading: false, err: "", reload: vi.fn(),
  })),
  useFundamentals: vi.fn(() => ({
    info: { name: "贵州茅台", industry: "白酒", concepts: ["消费"], high_limit: 11, low_limit: 9 },
    fin: { pe: 30 },
  })),
  formatPct: (v) => `${Number(v).toFixed(2)}%`,
  preCloseOf: () => 10,
  applyTickToBars: (bars) => bars,
}));
vi.mock("../../lib/quoteHub.jsx", () => ({
  useQuotes: vi.fn(() => ({ quotes: {}, state: "connected" })),
}));
vi.mock("../../lib/chartConfig.jsx", () => ({
  useChartSpec: () => ({ spec: {} }),
  mainIndicatorOptions: () => [{ v: "ma", label: "MA" }, { v: "boll", label: "BOLL" }, { v: "none", label: "无" }],
  subIndicatorOptions: () => [{ v: "macd", label: "MACD" }],
  broadcastIndicatorChange: vi.fn(),
}));
vi.mock("../../BrokerContext.jsx", () => ({
  useBroker: () => ({ activeId: null }),
}));

import MarketData from "../MarketData.jsx";
import { api } from "../../api.js";

vi.mock("../../api.js", () => ({
  api: {
    marketPeriods: vi.fn(() => Promise.resolve({ periods: [
      { v: "tick", label: "分时", kind: "tick", supported: true },
      { v: "1d", label: "日线", kind: "kline", supported: true },
      { v: "1q", label: "季线", kind: "kline", supported: false, reason: "无数据源" },
    ] })),
    marketAnalysis: vi.fn(() => Promise.resolve({ type: "stock", performance: {}, capital: {} })),
    marketKline: vi.fn(() => Promise.resolve({})),
    marketMinutes: vi.fn(() => Promise.resolve({ points: [] })),
    marketMoneyflow: vi.fn(() => Promise.resolve({})),
    marketCapital: vi.fn(() => Promise.resolve({})),
    moneyflowReplay: vi.fn(() => Promise.resolve({ rows: [] })),
    marketBoardLookup: vi.fn(() => Promise.resolve({ matches: [] })),
    marketBoardConstituents: vi.fn(() => Promise.resolve({ items: [] })),
  },
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

async function render(params = { code: "600519.SH" }) {
  await act(async () => {
    root.render(<MarketData params={params} leafId="leaf-1" tabId="tab-1" dispatch={vi.fn()} />);
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("MarketData 编排层（拆分后）", () => {
  it("渲染顶栏代码与 K 线图表", async () => {
    await render();
    expect(container.querySelector(".mp-code")?.textContent).toBe("600519.SH");
    expect(container.querySelector('[data-testid="chart"]')).toBeTruthy();
    expect(container.textContent).toContain("贵州茅台");
  });

  it("周期契约：不可用周期置灰 disabled", async () => {
    await render();
    const btns = [...container.querySelectorAll(".mp-pd")];
    const q = btns.find((b) => b.title?.includes?.("无数据源") || b.textContent === "季线");
    expect(q?.disabled).toBe(true);
  });

  it("订阅：回写 LEAF_PARAMS 且代码切换", async () => {
    const dispatch = vi.fn();
    await act(async () => {
      root.render(<MarketData params={{ code: "600519.SH" }} leafId="leaf-1" tabId="tab-1" dispatch={dispatch} />);
      await Promise.resolve();
      await Promise.resolve();
    });
    const input = container.querySelector(".mp-code-input");
    await act(async () => {
      // React 受控输入须用原生 setter 触发（直接改 value 不经过 React 状态）
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, "value").set;
      setter.call(input, "000001.SZ");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    const subscribeBtn = [...container.querySelectorAll("button")].find((b) => b.textContent === "订阅");
    await act(async () => { subscribeBtn.click(); await Promise.resolve(); });
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({
      type: "LEAF_PARAMS", params: { code: "000001.SZ" },
    }));
  });

  it("底部抽屉：五个页签均可切换且快速交易面板可开", async () => {
    await render();
    const tab = (label) => [...container.querySelectorAll(".mp-drawer-tab")]
      .find((b) => b.textContent === label);
    for (const [label, sel] of [
      ["五档盘口", '[data-testid="quote-panel"]'],
      ["快速交易", ".qt-card"],
      ["F10 财务", ".sa-f10"],
      ["多维摘要", ".mf-card"],
      ["同板块联动", ".link-card"],
    ]) {
      await act(async () => { tab(label).click(); await Promise.resolve(); });
      expect(container.querySelector(".mp-drawer-body")?.querySelector(sel)).toBeTruthy();
    }
    await act(async () => { tab("快速交易").click(); await Promise.resolve(); });
    expect([...container.querySelectorAll(".qt-side-btn")].map((b) => b.textContent))
      .toEqual(["买", "卖"]);
  });
});
