/**
 * 格式化工具。行情终端对数字格式极度敏感，统一在此收口。
 */

export function fmtPrice(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return v.toFixed(digits);
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function fmtSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
}

/** 中文数量级：万 / 亿 */
export function fmtAmount(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const abs = Math.abs(v);
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万`;
  return v.toFixed(0);
}

export function fmtVolume(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const abs = Math.abs(v);
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万`;
  return String(v);
}

/**
 * 涨跌方向：1 涨 / -1 跌 / 0 平。
 * 用于 DataTable 的 rowTone 与各处着色，避免各处重复判断。
 */
export function tone(v: number | null | undefined): -1 | 0 | 1 {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) return 0;
  return v > 0 ? 1 : -1;
}

/** 涨跌色 CSS 变量名，配合 inline style 使用 */
export function toneColor(v: number | null | undefined): string {
  const t = tone(v);
  if (t > 0) return "var(--up)";
  if (t < 0) return "var(--down)";
  return "var(--flat)";
}

/** 时间戳 → HH:MM:SS
 *
 * 后端时间戳契约（V11 R8 起唯一）：`YYYY-MM-DDTHH:MM:SS`（ISO 本地时间，无偏移）。
 * ECMAScript 规定「带时间但不带偏移」的串按**本地时间**解析（只有纯日期串才按 UTC），
 * 故此处直接 `new Date` 即可；存量带偏移/空格分隔的值也能被 V8 正确解析。
 */
export function fmtTime(ts: string | number | null | undefined): string {
  if (ts === null || ts === undefined || ts === "") return "--";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 交易时段判断（A 股：09:30-11:30 / 13:00-15:00） */
export function isTradingHours(now: Date = new Date()): boolean {
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const m = now.getHours() * 60 + now.getMinutes();
  return (m >= 570 && m <= 690) || (m >= 780 && m <= 900);
}

/** 代码规范化：600519 → 600519.SH */
export function normalizeCode(raw: string): string {
  const v = raw.trim().toUpperCase();
  if (/\.(SH|SZ|BJ)$/.test(v)) return v;
  if (/^6\d{5}$/.test(v)) return `${v}.SH`;
  if (/^(0|3)\d{5}$/.test(v)) return `${v}.SZ`;
  if (/^(4|8)\d{5}$/.test(v)) return `${v}.BJ`;
  return v;
}
