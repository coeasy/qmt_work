// nav 协议 v2 —— 全站导航的唯一入口。
//
// 旧版：window.dispatchEvent(new CustomEvent("nav", { detail: "quote" }))
//   仅携带 pageKey 字符串，无法带参数 → 「打开行情页再按 F3 点了没反应」的历史根因。
// 新版：navTo("quote", { params: { code: "000001.SH" }, openIn: "auto" })
//   detail 统一为对象，携带实例参数与打开方式；旧字符串调用点由兼容层自动归一化，
//   因此 FunctionTree / MenuBar / CommandPalette / Dashboard 等调用点无需同步修改。
import { PAGES, DEFAULT_PAGE } from "../pagesRegistry.jsx";

export const OPEN_IN = {
  AUTO: "auto",          // 智能路由（默认）
  TAB: "tab",            // 总是新开实例 tab 并激活
  BACKGROUND: "background", // 新开 tab 但不激活
  REPLACE: "replace",    // 更新当前激活叶子的参数（换股票 / 换周期）
  SPLIT_H: "split-h",    // 在当前 tab 中左右拆分出新窗口
  SPLIT_V: "split-v",    // 在当前 tab 中上下拆分出新窗口
};

// 归一化 nav 事件 detail：兼容字符串（旧调用点）与对象（新调用点）。
export function normalizeNavDetail(detail) {
  if (typeof detail === "string") {
    return PAGES[detail] ? { pageKey: detail, params: {}, openIn: OPEN_IN.AUTO } : null;
  }
  if (detail && typeof detail === "object" && typeof detail.pageKey === "string") {
    const key = PAGES[detail.pageKey] ? detail.pageKey : DEFAULT_PAGE;
    return {
      pageKey: key,
      params: detail.params && typeof detail.params === "object" ? detail.params : {},
      openIn: detail.openIn || OPEN_IN.AUTO,
    };
  }
  return null;
}

// 导航主入口。所有需要打开/切换页面的地方一律调用它。
export function navTo(pageKey, { params = {}, openIn = OPEN_IN.AUTO } = {}) {
  const norm = normalizeNavDetail({ pageKey, params, openIn });
  if (!norm) return false;
  window.dispatchEvent(new CustomEvent("nav", { detail: norm }));
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
