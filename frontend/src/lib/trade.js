// 快速交易跳转（涨停板 / 行情 / K 线共通）：
// 当用户已经在「交易」Hub 的任意子页（如涨停监控）时，直接 nav 到 "trade" 不会切换
// Hub 子页，导致 Trade 组件不挂载、trade:prefill 事件丢失 →「点击交易没反应」。
// 这里统一三步：1) 写入 pending（Trade 即便尚未挂载也能在挂载时读到）；
//              2) 触发 nav 切到交易入口；3) 通知 Trade Hub 切到「手动交易」子页签。
export function quickTradeNavigate(code, price, direction = "buy") {
  const payload = { code, price: price ? Number(price) : undefined, direction };
  window.__prefillTrade = payload;
  window.dispatchEvent(new CustomEvent("nav", { detail: "trade" }));
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