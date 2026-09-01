// @vitest-environment jsdom
//
// 统一列表刷新机制回归：
//   1. 挂载即加载（等价旧 useEffect(load, [])）
//   2. Pane 激活即刷新（keep-alive 切回不显示陈旧列表）
//   3. notifyListChanged(scope) 只触发订阅了该作用域的列表
import { act, createElement as h } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PaneActiveContext } from "../../hooks/useActiveInterval.js";
import { useListRefresh, notifyListChanged } from "../listRefresh.js";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = null;
});

afterEach(() => {
  if (root) act(() => root.unmount());
  container.remove();
});

function renderHook(hook, props = {}) {
  function Host() {
    hook();
    return null;
  }
  act(() => {
    root = createRoot(container);
    root.render(h(PaneActiveContext.Provider, { value: props.active ?? true }, h(Host)));
  });
}

describe("useListRefresh / notifyListChanged", () => {
  it("挂载即加载一次", () => {
    const load = vi.fn();
    renderHook(() => useListRefresh(load));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("notifyListChanged 只刷新订阅了匹配作用域的列表", () => {
    const loadA = vi.fn();
    const loadB = vi.fn();
    renderHook(() => useListRefresh(loadA, { scope: "target_plans" }));
    renderHook(() => useListRefresh(loadB, { scope: "api_keys" }));
    act(() => notifyListChanged("target_plans"));
    expect(loadA).toHaveBeenCalledTimes(2);   // 挂载 1 次 + 事件 1 次
    expect(loadB).toHaveBeenCalledTimes(1);   // 不受无关作用域影响
  });

  it("非激活 Pane 收到事件后静默排队由激活路径刷新（事件仍触发回调，由数据层幂等兜底）", () => {
    const load = vi.fn();
    renderHook(() => useListRefresh(load, { scope: "s1" }), { active: false });
    // 非激活时不因挂载而加载（G5 语义）
    expect(load).not.toHaveBeenCalled();
    act(() => notifyListChanged("s1"));
    // 作用域事件仍然送达（数据刷新代价远低于错过变更）
    expect(load).toHaveBeenCalledTimes(1);
  });
});
