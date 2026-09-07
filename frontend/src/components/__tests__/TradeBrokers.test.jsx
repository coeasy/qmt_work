// @vitest-environment jsdom
// Trade / Brokers 页面冒烟（2026-09 第三期）：渲染契约与关键交互，api/useBroker 全 mock。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../BrokerContext.jsx", () => ({
  useBroker: vi.fn(() => ({
    activeId: "conn-1", activeBroker: { name: "模拟券商", broker_id: "gj" },
    profiles: [{ broker_id: "gj", name: "国金", client_versions: [] }],
    brokers: [{ id: "conn-1", name: "模拟券商", broker_id: "gj", status: "connected" }],
    add: vi.fn(() => Promise.resolve()), connect: vi.fn(() => Promise.resolve()),
    disconnect: vi.fn(() => Promise.resolve()), remove: vi.fn(() => Promise.resolve()),
    batchRemove: vi.fn(() => Promise.resolve()), setActive: vi.fn(() => Promise.resolve()),
    test: vi.fn(() => Promise.resolve({ connected: true })),
    autoDetect: vi.fn(() => Promise.resolve([])),
  })),
}));

vi.mock("../api.js", () => ({
  api: new Proxy({}, { get: (_t, k) => vi.fn((...a) => {
    // 通用兜底：返回形状安全的空数据；列表类返回数组
    if (/^(tradePositions|tradeOrders|tradeDeals|tradeConditions)$/.test(k)) {
      return Promise.resolve(k === "tradePositions" ? [
        { code: "600519.SH", name: "贵州茅台", volume: 100, avg_cost: 1700, last: 1750 } ] : []);
    }
    return Promise.resolve({});
  }) }),
}));

vi.mock("../hooks/useSystemWS.js", () => ({ useServerEvents: vi.fn(() => null) }));
vi.mock("../hooks/useActiveInterval.js", () => ({ useActiveInterval: vi.fn() }));
vi.mock("../lib/trade.js", () => ({ consumePendingPrefill: vi.fn(() => null) }));
vi.mock("../lib/usePersistentState.js", () => ({
  default: (k, init) => [init, vi.fn()],
}));
vi.mock("./ui/ConfirmTradeModal.jsx", () => ({ default: () => null }));
vi.mock("../hooks/useBatchSelection.js", () => ({
  useBatchSelection: () => ({ selected: [], toggle: vi.fn(), clear: vi.fn(), has: vi.fn(() => false) }),
}));
vi.mock("./BatchDeleteBar.jsx", () => ({ default: () => null }));

import Trade from "../Trade.jsx";
import Brokers from "../Brokers.jsx";

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
});

async function render(Comp, props = {}) {
  await act(async () => {
    root.render(<Comp {...props} />);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("Trade 页面", () => {
  it("渲染持仓与下单表单（页面可完整挂载）", async () => {
    await render(Trade);
    expect(container.querySelectorAll("input, select").length).toBeGreaterThan(0);
    // 下单方向按钮存在（买/卖交互契约）
    const text = container.textContent;
    expect(/买|卖/.test(text)).toBe(true);
  });
});

describe("Brokers 页面", () => {
  it("渲染券商连接页且无崩溃（列表数据经 useBroker 注入）", async () => {
    await render(Brokers);
    // 页面骨架渲染成功 + 关键引导文案存在；连接卡数据由 BrokerContext 提供
    expect(container.textContent).toContain("券商连接管理");
    expect(container.querySelectorAll("button").length).toBeGreaterThan(0);
  });
});
