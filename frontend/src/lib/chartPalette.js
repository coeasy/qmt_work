// 图表调色板（T13）：全站 ECharts 配色单一真源。
//
// 与 styles.css :root 令牌同源（--up/--down/--accent/...），
// 供 ECharts 的 color/lineStyle/itemStyle 使用（CSS 变量不可直接喂 ECharts，
// 统一在此引用一次，避免各图表页硬编码 #ef4d56 等漂移）。
//
// 注意：A 股涨红跌绿约定 —— PALETTE.up 恒为红、down 恒为绿。

export const PALETTE = {
  up: "#ef4d56",        // 涨（K 线阳线 / 多军）—— 与 --up 同源但略亮（图表可读性）
  down: "#29c08a",      // 跌（K 线阴线 / 空军）
  upSoft: "rgba(239,77,86,.28)",
  downSoft: "rgba(41,192,138,.28)",
  accent: "#4f8cff",    // 主强调（选中/当前线）
  accent2: "#2bd4a4",   // 次强调
  warn: "#ffb020",
  danger: "#ff5c6c",
  // 主图叠加线（MA/BOLL 等）
  line: ["#ffffff", "#fdbb30", "#9b7bd8", "#91cc75", "#4f8cff"],
  // 副图 DIF/DEA/K/D/J/RSI 等
  sub: ["#ffffff", "#fdbb30", "#4f8cff", "#91cc75"],
  // 轴/文字/分割线
  axis: "#3a4a66",
  split: "rgba(58,74,102,.3)",
  text: "#e6ecf5",
  textDim: "#8a97ad",
  tooltipBg: "rgba(18,22,32,.92)",
  tooltipBorder: "#2c3850",
};

// 按涨跌语义取色（红涨绿跌，CN 惯例）
export function changeColor(v) {
  if (v == null) return PALETTE.textDim;
  return v >= 0 ? PALETTE.up : PALETTE.down;
}

// 主图指标 → 线色（对齐 GET /market/chart-spec 的 colors 顺序）
export const MAIN_LINE_COLORS = {
  ma: ["#ffffff", "#fdbb30", "#9b7bd8", "#91cc75"],
  boll: ["#c23531", "#91cc75", "#c23531"],
};

export const SUB_LINE_COLORS = {
  macd: ["#ffffff", "#fdbb30", "#ef4d56"],
  kdj: ["#ef4d56", "#fdbb30", "#4f8cff"],
  rsi: ["#ef4d56", "#4f8cff"],
  wr: ["#91cc75"],
};
