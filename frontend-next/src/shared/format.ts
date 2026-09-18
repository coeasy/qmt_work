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

/**
 * 金额（元）：大额走万/亿，**小额保留 2 位小数**。
 *
 * ⚠️ 与 `fmtAmount` 的区别不是洁癖，是一条真实缺陷：
 * `fmtAmount` 对 |v| < 1万 走 `toFixed(0)`，于是
 *   持仓市值 177.1 → "177"、浮动盈亏 -14.7 → "-15"
 * 用户拿它对券商对账单（**-14.70**）会发现**永远差一块钱**，且无从判断是算错还是显示错。
 * 成交额/成交量那种本来就是大数的列继续用 `fmtAmount` / `fmtVolume`；
 * **市值、盈亏、成本这类「账户金额」一律用 `fmtMoney`**。
 */
export function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const abs = Math.abs(v);
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万`;
  return v.toFixed(2);
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

/**
 * 标的「名称 + 代码」的显示契约。
 *
 * 返回 `[主标题, 副标题]`：名称已知时为主标题=名称、副标题=代码；
 * 名称未知时（盘前无行情、后端未回填 name）主标题回退为代码，
 * 且副标题返回 **null**，调用方必须**跳过代码槽**。
 *
 * ⚠️ 之所以把两者绑在一个函数里返回：曾经各写各的
 * （`{name ?? code}` + 无条件渲染 `{code}`），盘前 name 为空时
 * 同一个代码被渲染两遍、叠在一起，在左侧自选股面板表现为「重影」
 * （2026-09-17 实测截图确认）。绑在一起就不可能再漏掉一半。
 */
export function namePair(
  name: string | null | undefined,
  code: string,
): [string, string | null] {
  const n = name?.trim();
  return n ? [n, code] : [code, null];
}

/**
 * 订单状态 → 中文文案（**唯一入口**）。
 *
 * ★ 为什么必须有：此前 Trade 页直接 `{r.status}` 裸渲染英文串，用户看到
 * `partial` / `cancelled` 这类原始词；Positions 页另写一套映射且键名对不上
 * （`part_filled`），两边各说各话。键与后端平台标准词表严格一致
 * （backend/xtquant_client/order_status.py）。
 *
 * 未知值原样返回（绝不静默显示成"已成交"之类），便于立刻发现新状态。
 */
export function orderStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case "pending":
      return "待成交";
    case "partial":
      return "部分成交";
    case "filled":
      return "已成交";
    case "cancelled":
      return "已撤单";
    case "rejected":
      return "废单";
    case "unknown":
      return "未知";
    default:
      return status?.trim() ? status : "--";
  }
}

/** 订单状态 → Badge tone（与 orderStatusLabel 配套，避免各页重复三元表达式）。 */
export function orderStatusTone(
  status: string | null | undefined,
): "success" | "danger" | "neutral" | "info" | "warning" {
  switch (status) {
    case "filled":
      return "success";
    case "rejected":
      return "danger";
    case "partial":
      return "warning";
    case "cancelled":
    case "unknown":
      return "neutral";
    default:
      return "info";
  }
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
