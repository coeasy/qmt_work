// @vitest-environment jsdom
//
// G5 生命周期治理的回归保护。
// 锁定的契约（打破即等于把空转问题重新引入）：
//   1. 非激活 Pane：既不立即执行，也不建立定时器 —— 完全静默
//   2. 切回激活：立即执行一次（相当于刷新）+ 重启定时器
//   3. 不在任何 Pane 上下文中的组件：默认激活，行为与裸 setInterval 一致

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PaneActiveContext, useActiveInterval } from "../useActiveInterval.js";

// React 18 在测试环境需要这个标志，否则 act 会告警
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

/** 渲染一个使用 useActiveInterval 的探针组件，返回回调 spy 与控制句柄。 */
function mountProbe({ delay = 1000, active, deps = [], options } = {}) {
  const spy = vi.fn();
  const setActive = { fn: null };
  function Probe() {
    useActiveInterval(spy, delay, deps, options);
    return null;
  }
  function Harness({ isActive }) {
    return (
      <PaneActiveContext.Provider value={isActive}>
        <Probe />
      </PaneActiveContext.Provider>
    );
  }
  function render(isActive) {
    act(() => root.render(<Harness isActive={isActive} />));
  }
  // 未提供 active 时不包裹 Provider，走默认 context（true）
  function renderDefault() {
    act(() => root.render(<Probe />));
  }
  setActive.fn = render;
  if (active === undefined) renderDefault();
  else render(active);
  return { spy, setActive };
}

describe("useActiveInterval", () => {
  it("激活时立即执行一次并建立定时器", () => {
    vi.useFakeTimers();
    const { spy } = mountProbe({ delay: 1000, active: true });
    expect(spy).toHaveBeenCalledTimes(1); // immediate
    act(() => { vi.advanceTimersByTime(3000); });
    expect(spy).toHaveBeenCalledTimes(4); // + 3 次 tick
    vi.useRealTimers();
  });

  it("非激活 Pane 完全静默：既不立即执行也不建定时器", () => {
    vi.useFakeTimers();
    const { spy } = mountProbe({ delay: 1000, active: false });
    expect(spy).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(10000); });
    expect(spy).not.toHaveBeenCalled(); // 后台 10s 内一次都没打后端
    vi.useRealTimers();
  });

  it("切回激活时立即补刷新并重启定时器", () => {
    vi.useFakeTimers();
    const { spy, setActive } = mountProbe({ delay: 1000, active: false });
    expect(spy).not.toHaveBeenCalled();
    setActive.fn(true); // 切回
    expect(spy).toHaveBeenCalledTimes(1); // 补一次刷新
    act(() => { vi.advanceTimersByTime(2000); });
    expect(spy).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });

  it("切走后定时器被拆除，不再继续触发", () => {
    vi.useFakeTimers();
    const { spy, setActive } = mountProbe({ delay: 1000, active: true });
    act(() => { vi.advanceTimersByTime(2000); });
    const before = spy.mock.calls.length;
    setActive.fn(false);
    act(() => { vi.advanceTimersByTime(10000); });
    expect(spy.mock.calls.length).toBe(before); // 切走后一次都没再触发
    vi.useRealTimers();
  });

  it("不在 Pane 上下文中时默认激活（行为不变）", () => {
    vi.useFakeTimers();
    const { spy } = mountProbe({ delay: 1000 }); // 不包裹 Provider
    expect(spy).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(2000); });
    expect(spy).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });

  it("immediate=false 时不预执行，只按间隔触发", () => {
    vi.useFakeTimers();
    const { spy } = mountProbe({ delay: 1000, active: true, options: { immediate: false } });
    expect(spy).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(spy).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("delay<=0 时只执行一次，不建立常驻定时器", () => {
    vi.useFakeTimers();
    const { spy } = mountProbe({ delay: 0, active: true });
    expect(spy).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(10000); });
    expect(spy).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("deps 变化会重启定时器", () => {
    vi.useFakeTimers();
    const spy = vi.fn();
    function Probe({ dep }) {
      useActiveInterval(spy, 1000, [dep]);
      return null;
    }
    act(() => root.render(<Probe dep={1} />));
    act(() => { vi.advanceTimersByTime(1000); });
    expect(spy).toHaveBeenCalledTimes(2);
    act(() => root.render(<Probe dep={2} />)); // deps 变化 -> 重启并立即执行
    expect(spy).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });

  it("始终调用最新回调（不会冻结首帧闭包）", () => {
    vi.useFakeTimers();
    const first = vi.fn();
    const second = vi.fn();
    function Probe({ fn }) {
      useActiveInterval(fn, 1000);
      return null;
    }
    act(() => root.render(<Probe fn={first} />));
    act(() => root.render(<Probe fn={second} />));
    act(() => { vi.advanceTimersByTime(1000); });
    expect(second).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
