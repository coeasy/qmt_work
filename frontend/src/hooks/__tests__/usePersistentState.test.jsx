// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import usePersistentState from "../../lib/usePersistentState.js";

// T20 持久化 Tab：localStorage 读写 / 默认值 / 节流写入 / 损坏数据回退
// React 18 测试环境标志（对齐 useActiveInterval.test.jsx 模式）
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
  act(() => root.unmount());
  container.remove();
});

// 极简 renderHook（无 @testing-library 依赖）：渲染调用 hook 的探针组件，
// 通过 setter 同步最新返回值供断言。
function mountHook(initialKey, initial) {
  let latest = null;
  function Probe() {
    latest = usePersistentState(initialKey, initial);
    return null;
  }
  act(() => root.render(<Probe />));
  return { result: { get current() { return latest; } } };
}

describe("usePersistentState", () => {
  it("无存量 → 用默认值", () => {
    const { result } = mountHook("test:tab", "order");
    expect(result.current[0]).toBe("order");
  });

  it("有存量 → 恢复", () => {
    localStorage.setItem("test:tab", JSON.stringify("settings"));
    const { result } = mountHook("test:tab", "order");
    expect(result.current[0]).toBe("settings");
  });

  it("损坏数据 → 回退默认值不崩", () => {
    localStorage.setItem("test:tab", "{broken json");
    const { result } = mountHook("test:tab", "order");
    expect(result.current[0]).toBe("order");
  });

  it("set → state 更新 + 节流后写入 localStorage", async () => {
    vi.useFakeTimers();
    const { result } = mountHook("test:tab", "order");
    act(() => result.current[1]("board"));
    expect(result.current[0]).toBe("board");
    await vi.advanceTimersByTimeAsync(300);
    expect(localStorage.getItem("test:tab")).toBe(JSON.stringify("board"));
  });

  it("对象值 roundtrip", async () => {
    vi.useFakeTimers();
    const { result } = mountHook("test:filters", { a: 1 });
    act(() => result.current[1]((prev) => ({ ...prev, b: 2 })));
    await vi.advanceTimersByTimeAsync(300);
    expect(JSON.parse(localStorage.getItem("test:filters"))).toEqual({ a: 1, b: 2 });
  });
});
