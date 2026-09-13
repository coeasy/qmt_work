import type { Period } from "./types";

/**
 * 周期定义与标签（独立模块）。
 *
 * 为什么单独抽出来：Toolbar 需要周期标签，但不应因此把 klinecharts（体积大）
 * 拉进主包。把标签放在此无依赖模块，图表库只随图表页面按需加载。
 */

export type KLinePeriodType =
  | "second"
  | "minute"
  | "hour"
  | "day"
  | "week"
  | "month"
  | "year";

export const PERIODS: Period[] = ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M"];

export const PERIOD_LABELS: Record<Period, string> = {
  "1m": "1分",
  "5m": "5分",
  "15m": "15分",
  "30m": "30分",
  "60m": "60分",
  "1d": "日线",
  "1w": "周线",
  "1M": "月线",
};

/** 平台周期 → klinecharts 周期 */
export const PERIOD_MAP: Record<Period, { type: KLinePeriodType; span: number }> = {
  "1m": { type: "minute", span: 1 },
  "5m": { type: "minute", span: 5 },
  "15m": { type: "minute", span: 15 },
  "30m": { type: "minute", span: 30 },
  "60m": { type: "hour", span: 1 },
  "1d": { type: "day", span: 1 },
  "1w": { type: "week", span: 1 },
  "1M": { type: "month", span: 1 },
};

/** 周期循环（F5 快捷键用） */
export function nextPeriod(cur: Period): Period {
  const i = PERIODS.indexOf(cur);
  return PERIODS[(i + 1) % PERIODS.length] ?? "1d";
}
