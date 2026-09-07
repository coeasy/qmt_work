// eventBus —— 显式应用事件总线（H5：取代 window.dispatchEvent/CustomEvent）。
//
// 此前跨组件通信用 window CustomEvent：事件名是裸字符串散落 10+ 组件、
// 数据流不可追踪、无法单测。本模块提供同语义的显式总线：
//
//   import { on, emit } from "../lib/eventBus";
//   const off = on("nav", (detail) => { ... });   // detail 即 CustomEvent.detail
//   emit("nav", { pageKey: "quote" });            // 返回是否有订阅者
//   off();                                        // 或 off("nav", fn)
//
// 约定：
//   · 只承载「应用级」事件（nav/列表刷新/命令面板等）；DOM 原生事件
//     （keydown/resize 等）仍走 window 监听，不进总线。
//   · 事件名沿用既有命名（含 nav / qmt:* / cmd:* / help:* / hub:* 等），
//     全站清单见 lib/eventBus.js 底部注释（单一事实源）。
//
// 全站事件清单：
//   nav                    —— 页面导航（lib/nav.js 是唯一 emit 入口）
//   qmt:list-changed       —— 列表作用域刷新广播（lib/listRefresh.js）
//   qmt:watch:update       —— 自选股变更
//   qmt:chart:indicator    —— 图表指标开关
//   trade:prefill          —— 交易页预填
//   sa:toggle-period       —— 单股分析周期切换
//   qb:switch              —— QuoteBoard 视图切换
//   hub:switch             —— Hub 页签切换
//   cmd:toggle / cmd:close —— 命令面板开合
//   help:toggle / help:close —— 帮助浮层开合
const handlers = new Map();   // event -> Set<fn>

export function on(event, fn) {
  if (!event || typeof fn !== "function") return () => {};
  let set = handlers.get(event);
  if (!set) { set = new Set(); handlers.set(event, set); }
  set.add(fn);
  return () => off(event, fn);
}

export function off(event, fn) {
  const set = handlers.get(event);
  if (set) {
    set.delete(fn);
    if (!set.size) handlers.delete(event);
  }
}

// 触发事件；detail 透传给每个订阅者（等价旧 CustomEvent.detail）。
// 返回是否有订阅者收到（便于调用方在无人订阅时走兜底逻辑）。
export function emit(event, detail) {
  const set = handlers.get(event);
  if (!set || !set.size) return false;
  [...set].forEach((fn) => {
    try { fn(detail); } catch (err) { console.error(`[eventBus] ${event} handler error`, err); }
  });
  return true;
}

// 测试/热更用：清空全部订阅。
export function clear() { handlers.clear(); }
