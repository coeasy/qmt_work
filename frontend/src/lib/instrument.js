// 标的类型徽章（与后端 app/datasource/instrument.py 分类对应）：
// ETF 蓝 / 股 灰 / 指 紫 / 板块 橙 / 债 青（配色见 styles.css .gbadge.*）
export const TYPE_BADGE = {
  stock: { label: "股", cls: "stock" },
  etf: { label: "ETF", cls: "etf" },
  index: { label: "指", cls: "index" },
  board: { label: "板块", cls: "board" },
  bond: { label: "债", cls: "bond" },
};

export const badgeOf = (t) => TYPE_BADGE[t] || { label: "标的", cls: "other" };
