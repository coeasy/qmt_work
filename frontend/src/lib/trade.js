// 快速交易跳转（行情五档/涨停板/K 线共通）：
// v3 主通道走 nav 协议（navTo("trade", { params })）：
//   · 当前叶子已是交易 → replace 就地预填（根治「点了没反应」）
//   · 否则打开/激活交易实例，params 由 Pane 直达组件
// 兼容兜底保留 trade:prefill 事件 + pending（Trade Hub 子页切换仍依赖事件）。
import { navTo } from "./nav.js";

export function quickTradeNavigate(code, price, direction = "buy") {
  const payload = { code, price: price ? Number(price) : undefined, direction };
  // pending 兜底：Trade 尚未挂载时也能在挂载瞬间读到，避免竞态丢单
  window.__prefillTrade = payload;
  navTo("trade", { params: payload });
  window.dispatchEvent(new CustomEvent("hub:switch", {
    detail: { hub: "trade", tab: "trade" },
  }));
  window.dispatchEvent(new CustomEvent("trade:prefill", { detail: payload }));
}

export function consumePendingPrefill() {
  const p = window.__prefillTrade;
  delete window.__prefillTrade;
  return p;
}
