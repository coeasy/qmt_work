// nav 协议 v2 —— 全站导航的唯一入口。
//
// 旧版：window.dispatchEvent(new CustomEvent("nav", { detail: "quote" }))
//   仅携带 pageKey 字符串，无法带参数 → 「打开行情页再按 F3 点了没反应」的历史根因。
// 新版：navTo("quote", { params: { code: "000001.SH" }, openIn: "auto" })
//   detail 统一为对象，携带实例参数与打开方式；旧字符串调用点由兼容层自动归一化，
//   因此 MenuBar / CommandPalette / Dashboard 等调用点无需同步修改。
import { PAGES, DEFAULT_PAGE, resolveKey, aliasTab, KEY_ALIAS } from "../pagesRegistry.jsx";
import { emit as emitEvent } from "./eventBus";

export const OPEN_IN = {
  AUTO: "auto",          // 智能路由（默认）
  TAB: "tab",            // 总是新开实例 tab 并激活
  BACKGROUND: "background", // 新开 tab 但不激活
  REPLACE: "replace",    // 更新当前激活叶子的参数（换股票 / 换周期）
  SPLIT_H: "split-h",    // 在当前 tab 中左右拆分出新窗口
  SPLIT_V: "split-v",    // 在当前 tab 中上下拆分出新窗口
};

// 归一化 nav 事件 detail：兼容字符串（旧调用点）与对象（新调用点）。
// 阶段一：被并入中心页的旧 key（KEY_ALIAS）在此统一归一为中心页 key，
// 并把旧 key 写入 params.tab 以定位到对应子页签。
export function normalizeNavDetail(detail) {
  if (typeof detail === "string") {
    if (!(PAGES[detail] || KEY_ALIAS[detail])) return null;
    const tab = aliasTab(detail);
    return { pageKey: resolveKey(detail), params: tab ? { tab } : {}, openIn: OPEN_IN.AUTO };
  }
  if (detail && typeof detail === "object" && typeof detail.pageKey === "string") {
    if (!(PAGES[detail.pageKey] || KEY_ALIAS[detail.pageKey])) {
      return { pageKey: DEFAULT_PAGE, params: {}, openIn: detail.openIn || OPEN_IN.AUTO };
    }
    const tab = aliasTab(detail.pageKey);
    const params = detail.params && typeof detail.params === "object" ? detail.params : {};
    const merged = tab ? { ...params, tab: params.tab || tab } : params;
    return {
      pageKey: resolveKey(detail.pageKey),
      params: merged,
      openIn: detail.openIn || OPEN_IN.AUTO,
    };
  }
  return null;
}

// 导航主入口。所有需要打开/切换页面的地方一律调用它。
export function navTo(pageKey, { params = {}, openIn = OPEN_IN.AUTO } = {}) {
  const norm = normalizeNavDetail({ pageKey, params, openIn });
  if (!norm) return false;
  emitEvent("nav", norm);
  return true;
}

// 换股票 / 换周期等「就地在当前激活窗口生效」的场景。
export function navReplace(params) {
  return navTo(undefined, { params, openIn: OPEN_IN.REPLACE });
}

// 便捷别名：打开行情分析并直接定位到某只股票。
export function navToQuote(code, { period, openIn = OPEN_IN.AUTO } = {}) {
  return navTo("quote", { params: { code, ...(period ? { period } : {}) }, openIn });
}

// 便捷别名：打开手动交易并预填委托参数（取代 window.__prefillTrade）。
export function navToTrade(code, price, direction = "buy") {
  const payload = { code, direction, ...(price ? { price: Number(price) } : {}) };
  return navTo("trade", { params: payload, openIn: OPEN_IN.REPLACE });
}

// ---------------------------------------------------------------------------
// 测试桥（E2E 冒烟用）。
//
// 导航事件走模块内 eventBus，而 page.evaluate 处于页面全局作用域，
// 拿不到模块闭包 → 冒烟脚本此前只能 window.dispatchEvent(new CustomEvent("nav"))
// 而全站无任何 window "nav" 监听者，导致**派发静默失效、始终停留在仪表盘**，
// 测试却全绿（假通过）。这里显式暴露 navTo 供自动化驱动真实导航路径。
//
// 只暴露导航函数，不暴露 eventBus 本体，避免测试绕过 nav 契约直接 emit 脏 detail。
if (typeof window !== "undefined") {
  window.__qmtNavTo = (pageKey, opts) => navTo(pageKey, opts || {});
  window.__qmtNavToQuote = (code, opts) => navToQuote(code, opts || {});
  window.__qmtResolveKey = (key) => resolveKey(key);
}
