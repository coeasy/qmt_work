// 行情分析页常量与纯工具（自 MarketData.jsx 原样拆出，行为零变更）。
import { PALETTE } from "../../lib/chartPalette.js";

export const PERIODS = [
  { v: "tick", label: "分时", kind: "tick" },
  { v: "1m",  label: "1分",  kind: "minute" },
  { v: "5m",  label: "5分",  kind: "minute" },
  { v: "15m", label: "15分", kind: "minute" },
  { v: "30m", label: "30分", kind: "minute" },
  { v: "60m", label: "60分", kind: "minute" },
  { v: "1d",  label: "日线", kind: "kline" },
  { v: "1w",  label: "周线", kind: "kline" },
  { v: "1mo", label: "月线", kind: "kline" },
  { v: "1q",  label: "季线", kind: "kline" },
  { v: "1y",  label: "年线", kind: "kline" },
];
export const ADJ_TYPES = [
  { v: "",    label: "不复权" },
  { v: "qfq", label: "前复权" },
  { v: "hfq", label: "后复权" },
];
export const MAIN_INDICATORS = [
  { v: "ma",   label: "MA" },
  { v: "boll", label: "BOLL" },
  { v: "none", label: "无" },
];
export const SUB_INDICATORS = [
  { v: "macd", label: "MACD" },
  { v: "kdj",  label: "KDJ" },
  { v: "rsi",  label: "RSI" },
  { v: "vol",  label: "VOL" },
  { v: "wr",   label: "WR" },
  { v: "none", label: "无" },
];
export const MA_PERIODS = [5, 10, 20, 60];
export const MA_COLORS = PALETTE.line; // 白/黄/紫/绿（chartPalette 单一真源）
export const SRC_LABEL = {
  eltdx: "通达信(TDX)行情",
  broker: "券商",
  cache: "本地缓存",
  cache_stale: "本地缓存(过期)",
};
export const DEFAULT_VOLUME = 100;     // 默认委托手数
export const PRICE_TICKS = [
  { v: "limit_up", label: "涨停价" },
  { v: "ask1",     label: "卖一价" },
  { v: "last",     label: "最新价" },
  { v: "bid1",     label: "买一价" },
  { v: "limit_dn", label: "跌停价" },
];
// 抽屉展开/收起时图表高度自适应（收起更高，给 K 线更多空间）
export const DRAWER_CHART_H = { open: 460, closed: 640 };

// 标的画像条（检索分析体系③）：/market/analysis 结果的模块级缓存，
// code → {ts, data}，30s TTL —— 切回已看标的秒出，不重复打后端。
export const ANALYSIS_TTL = 30000;
export const analysisCache = new Map();

export function toDate(s) {
  if (!s) return "";
  return String(s).slice(0, 10);
}
