// @vitest-environment jsdom
// TargetPortfolio 同步流回归（P0：dry_run 复选框 e.checked bug —— 取消勾选后必须仍为 false）。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({
  targetPlans: vi.fn(() => Promise.resolve([])),
  targetSync: vi.fn(() => Promise.resolve({})),
  signalMode: vi.fn(() => Promise.resolve({ mode: "live" })),
  brokerAccounts: vi.fn(() => Promise.resolve([])),
}));

vi.mock("../../api.js", () => ({ api: apiMock }));

import TargetPortfolio from "../TargetPortfolio.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("TargetPortfolio 模拟运行（dry_run）", () => {
  it("渲染模拟运行复选框且默认勾选", async () => {
    await act(async () => {
      root.render(<TargetPortfolio />);
      await Promise.resolve();
      await Promise.resolve();
    });
    const cb = container.querySelector('input[type="checkbox"]');
    expect(cb).toBeTruthy();
    expect(cb.checked).toBe(true);
  });

  it("取消勾选再勾选后 checked 状态正确翻转（e.target.checked 契约）", async () => {
    await act(async () => {
      root.render(<TargetPortfolio />);
      await Promise.resolve();
      await Promise.resolve();
    });
    const cb = container.querySelector('input[type="checkbox"]');
    await act(async () => { cb.click(); });
    expect(cb.checked).toBe(false);   // P0 修复前：e.checked 恒 undefined → 状态被污染
    await act(async () => { cb.click(); });
    expect(cb.checked).toBe(true);
  });
});
