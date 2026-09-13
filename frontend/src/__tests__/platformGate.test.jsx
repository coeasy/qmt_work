// @vitest-environment jsdom
// 能力门控 fail-closed 契约（V10 Phase C 收口）：
//   1) Provider 之外调用 usePlatform 不得抛异常（避免整页白屏）；
//   2) 交易类能力在「未连接券商」或「后端未声明」时一律为 false（绝不误开放真实下单）；
//   3) 研究/数据类能力保持可用（D12：无券商也能选股）；
//   4) 交易可用性 = 后端声明 ∩ 已连接券商（两个前提缺一不可）。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// 连接态可控：PlatformProvider 只关心 connectedCount / activeId
const brokerState = { connectedCount: 0, activeId: null };
vi.mock("../BrokerContext.jsx", () => ({
  useBroker: () => ({ ...brokerState }),
}));

// 后端 platform/status 与 capabilities 声明可控
const apiState = { platform: null, manifest: {} };
vi.mock("../api.js", () => ({
  api: {
    capabilitiesSummary: vi.fn(() => Promise.resolve(apiState.manifest)),
    platformStatus: vi.fn(() => Promise.resolve(apiState.platform)),
  },
}));

import { FALLBACK_PLATFORM, PlatformProvider, usePlatform } from "../PlatformContext.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let captured;

function Probe() {
  captured = usePlatform();
  return null;
}

beforeEach(() => {
  brokerState.connectedCount = 0;
  brokerState.activeId = null;
  apiState.platform = null;
  apiState.manifest = {};
  captured = undefined;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function renderTree(node) {
  await act(async () => {
    root.render(node);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("usePlatform fail-closed", () => {
  it("Provider 之外：不抛异常，交易能力为 false，研究能力可用", async () => {
    await renderTree(<Probe />);
    expect(captured).toBeTruthy();
    expect(captured.can("trading")).toBe(false);
    expect(captured.can("realtimeTrading")).toBe(false);
    // D12：研究/数据类不依赖券商连接，兜底时保持可用
    expect(captured.can("research")).toBe(true);
    expect(captured.can("data")).toBe(true);
    // 未知能力键 → fail-closed
    expect(captured.can("never-declared-capability")).toBe(false);
    expect(captured.connectorReady).toBe(false);
    expect(captured.activeConnectionId).toBeNull();
    expect(captured.ready).toBe(false);
  });

  it("兜底上下文为冻结单例（避免下游误改）", async () => {
    await renderTree(<Probe />);
    expect(captured).toBe(FALLBACK_PLATFORM);
    expect(Object.isFrozen(captured.capabilities)).toBe(true);
    expect(Object.isFrozen(captured.screeningProviders)).toBe(true);
  });

  it("已连接 + 后端声明 trading：can('trading') 为 true", async () => {
    brokerState.connectedCount = 1;
    brokerState.activeId = "conn-1";
    apiState.platform = { capabilities: { trading: true, realtimeTrading: true } };
    await renderTree(<PlatformProvider><Probe /></PlatformProvider>);
    expect(captured.can("trading")).toBe(true);
    expect(captured.can("realtimeTrading")).toBe(true);
    expect(captured.connectorReady).toBe(true);
    expect(captured.activeConnectionId).toBe("conn-1");
    expect(captured.ready).toBe(true);
  });

  it("后端声明 trading 但未连接券商：仍为 false（连接态是硬前提）", async () => {
    apiState.platform = { capabilities: { trading: true, realtimeTrading: true } };
    await renderTree(<PlatformProvider><Probe /></PlatformProvider>);
    expect(captured.can("trading")).toBe(false);
    expect(captured.can("realtimeTrading")).toBe(false);
  });

  it("已连接但后端未声明 trading：不得默认放开", async () => {
    brokerState.connectedCount = 1;
    brokerState.activeId = "conn-1";
    apiState.platform = { capabilities: {} };
    await renderTree(<PlatformProvider><Probe /></PlatformProvider>);
    expect(captured.can("trading")).toBe(false);
  });

  it("screeningReady 透传后端来源就绪状态", async () => {
    apiState.platform = { sources: { screening_ready: true, screening_providers: ["eltdx", "baostock"] } };
    await renderTree(<PlatformProvider><Probe /></PlatformProvider>);
    expect(captured.screeningReady).toBe(true);
    expect(captured.screeningProviders).toEqual(["eltdx", "baostock"]);
  });

  it("后端不可达（capabilities/platform 均失败）：降级为未就绪且交易不可用", async () => {
    apiState.platform = null;
    apiState.manifest = {};
    brokerState.connectedCount = 1;
    brokerState.activeId = "conn-1";
    await renderTree(<PlatformProvider><Probe /></PlatformProvider>);
    expect(captured.ready).toBe(true); // 请求返回（null）后 ready 置位，不悬挂
    expect(captured.can("trading")).toBe(false);
    expect(captured.screeningReady).toBe(false);
  });
});
