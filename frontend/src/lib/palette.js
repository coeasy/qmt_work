// 统一设计令牌（v2）：CSS 变量 + 图表配色的单一真相来源。
// 所有颜色从此文件导出，CSS 通过 :root 注入，ECharts 直接引用 JS 常量。
// 涨红跌绿（A 股惯例）。

export const COLORS = {
  // 背景
  bg: "#0f1420",
  bg2: "#161c2c",
  bg3: "#1c2336",
  panel: "#1b2333",
  panel2: "#222c40",
  // 边框
  border: "#2c3850",
  // 文字
  text: "#e6ecf5",
  textDim: "#8a97ad",
  // 强调色
  accent: "#4f8cff",
  accent2: "#2bd4a4",
  // 状态
  danger: "#ff5c6c",
  warn: "#ffb020",
  // 涨跌（红涨绿跌）
  up: "#ff4d4f",
  down: "#19c37d",
  upSoft: "rgba(255, 77, 79, 0.05)",
  downSoft: "rgba(25, 195, 125, 0.05)",
  upBar: "rgba(255, 77, 79, 0.28)",
  downBar: "rgba(25, 195, 125, 0.28)",
  // 图表专用（ECharts 可读性略调）
  chartUp: "#ef4d56",
  chartDown: "#29c08a",
  chartUpSoft: "rgba(239,77,86,0.28)",
  chartDownSoft: "rgba(41,192,138,0.28)",
  chartLine: ["#ffffff", "#fdbb30", "#9b7bd8", "#91cc75", "#4f8cff"],
  chartSub: ["#ffffff", "#fdbb30", "#4f8cff", "#91cc75"],
  chartAxis: "#3a4a66",
  chartSplit: "rgba(58,74,102,0.3)",
  chartTooltipBg: "rgba(18,22,32,0.92)",
  chartTooltipBorder: "#2c3850",
};

// 按涨跌语义取色（红涨绿跌，CN 惯例）
export function changeColor(v) {
  if (v == null) return COLORS.textDim;
  return v >= 0 ? COLORS.chartUp : COLORS.chartDown;
}

// CSS :root 变量注入（页面加载时调用一次）
export function injectCSSVars() {
  const el = document.documentElement;
  const map = {
    "--bg": COLORS.bg, "--bg-2": COLORS.bg2, "--bg-3": COLORS.bg3,
    "--panel": COLORS.panel, "--panel-2": COLORS.panel2, "--border": COLORS.border,
    "--text": COLORS.text, "--text-dim": COLORS.textDim,
    "--accent": COLORS.accent, "--accent-2": COLORS.accent2,
    "--danger": COLORS.danger, "--warn": COLORS.warn,
    "--up": COLORS.up, "--down": COLORS.down,
    "--up-soft": COLORS.upSoft, "--down-soft": COLORS.downSoft,
    "--up-bar": COLORS.upBar, "--down-bar": COLORS.downBar,
  };
  for (const [k, v] of Object.entries(map)) el.style.setProperty(k, v);
}

// ECharts 通用配置片段
export const CHART_THEME = {
  color: COLORS.chartLine,
  backgroundColor: "transparent",
  textStyle: { color: COLORS.text, fontFamily: '"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif' },
  grid: { left: 50, right: 20, top: 30, bottom: 24 },
  xAxis: { axisLine: { lineStyle: { color: COLORS.chartAxis } }, axisLabel: { color: COLORS.textDim } },
  yAxis: { axisLine: { lineStyle: { color: COLORS.chartAxis } }, axisLabel: { color: COLORS.textDim }, splitLine: { lineStyle: { color: COLORS.chartSplit } } },
};

// 主图指标线色
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
