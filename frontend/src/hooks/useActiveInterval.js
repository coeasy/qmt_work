// G5 生命周期治理：Pane 可见性感知的定时器。
//
// 背景（终极整合方案 G5）——一个真实且此前未被发现的性能问题：
//   workspace.jsx 的 MAX_ALIVE=8 让最多 8 个 Tab 常驻，Workbench.jsx 用
//   visibility:hidden 隐藏非激活 Pane 而**不卸载**，但全仓对
//   visibilityState / document.hidden / IntersectionObserver 的命中为 0。
//   结果：用户切到「交易」页时，背后最多 7 个页面的定时器仍在持续打后端，
//   后端限流、行情源配额、CPU 都在为空转付费。
// 参照 Fincept Terminal 的 P3 规则与 active_for_work 属性：定时器必须在
// show/hide 时启停，后台窗口抑制投递。本文件是该规则在 React 下的等价实现。
//
// 语义（重要）：
//   - 非激活 Pane：**既不立即执行，也不建立定时器**（完全静默）
//   - 切回激活：立即执行一次（相当于刷新）+ 重启定时器
//   - 未处于任何 Pane 上下文中的组件（如全局 Provider）：默认激活，行为不变

import { createContext, useContext, useEffect, useRef } from "react";

/** 当前 Pane 是否激活。默认值 true —— 保证 Pane 之外的组件行为完全不变。 */
export const PaneActiveContext = createContext(true);

/** 读取所在 Pane 的激活状态（不在 Pane 内时恒为 true）。 */
export function useIsPaneActive() {
  return useContext(PaneActiveContext);
}

/**
 * 可见性感知的 setInterval 替代品。
 *
 * @param {Function} fn        每次触发执行的回调（内部用 ref 持有，始终取最新闭包）
 * @param {number}   delay     间隔毫秒；<=0 时只执行 immediate 一次，不建定时器
 * @param {Array}    deps      额外依赖，变化后重启定时器（语义等同 useEffect deps）
 * @param {Object}   options   { immediate = true } 激活时是否先立即执行一次
 *
 * 用法（替换原本的 useEffect + setInterval 组合）：
 *   前：useEffect(() => { load(); const t = setInterval(load, 30000);
 *                          return () => clearInterval(t); }, []);
 *   后：useActiveInterval(load, 30000);
 */
export function useActiveInterval(fn, delay, deps = [], options = {}) {
  const { immediate = true } = options;
  const active = useIsPaneActive();
  const fnRef = useRef(fn);
  // 每次渲染刷新 ref：避免 effect 冻结首帧闭包而一直调用旧函数（经典陷阱）
  fnRef.current = fn;

  useEffect(() => {
    if (!active) return; // 后台静默：不刷新、不建定时器
    if (immediate) fnRef.current();
    if (!delay || delay <= 0) return;
    const t = setInterval(() => fnRef.current(), delay);
    return () => clearInterval(t);
    // deps 由调用方传入并展开，长度可变，故交由调用方保证完整性
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, delay, immediate, ...deps]);
}
