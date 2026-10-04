/**
 * 图表配色的**令牌解析** —— Canvas 渲染器下 `var(--x)` 不生效的根治入口。
 *
 * ## 为什么需要它（2026-10-03 实测）
 * ECharts 走 **CanvasRenderer**（`echartsSetup.ts`），而 Canvas **不解析 CSS 自定义属性**：
 * 把 `var(--up)` 原样写进 option，zrender 的颜色解析器会拿到 `undefined`
 * （实测 `zrender/lib/tool/color.js` 的 `parse("var(--up)") === undefined`；
 * echarts/zrender 全仓 grep `customProp|var(--|cssVar` 零命中）。
 *
 * 后果是**静默失效**：不报错、不告警，图表照常画 —— 只是颜色永远是图库/兜底色。
 * 于是
 *   - 涨跌配色切到「红涨蓝跌」后，K 线（klinecharts，用 `getComputedStyle` 取真值）
 *     变蓝了，而分时 / 资金流 / 市场结构 / 板块雷达（ECharts）**纹丝不动**；
 *   - 浅色主题下这些图表的坐标轴、网格仍是深色令牌的字符串（无法解析 ⇒ 用兜底）。
 *
 * ## 修法
 * 调用方照旧写 `var(--up)`（保持「组件不硬编码颜色」的约定），由 :func:`resolveCssVars`
 * 在 `setOption` **之前**统一替换成真实色值。这样收益面覆盖**所有** ECharts 调用方，
 * 不需要逐个文件改字符串，也不会出现「改了 3 个、漏了第 4 个」的漂移。
 *
 * ⚠️ 递归转换必须**原样透传**非纯对象的值（函数 formatter / class 实例），
 *    只重建 plain object 与数组 —— 深拷贝会破坏 formatter。
 */
const VAR_RE = /var\(\s*(--[\w-]+)\s*(?:,\s*([^)]+))?\)/g;

/** 取一个令牌的真实值；取不到时用调用方给的兜底（绝不留 `var(...)` 原串）。 */
export function cssVar(name: string, fallback = ""): string {
  if (typeof document === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** 把字符串里所有 `var(--x[, fallback])` 替换成真实色值（非字符串原样返回）。 */
export function resolveCssVarsInString(text: string): string {
  if (text.indexOf("var(") < 0) return text;
  return text.replace(VAR_RE, (_m, name: string, fb?: string) => cssVar(name, (fb ?? "").trim()));
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  if (v === null || typeof v !== "object") return false;
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

/**
 * 递归把 option 里的 `var(--x)` 换成真实色值。
 *
 * 只重建 plain object / 数组；函数、Date、class 实例、ECharts 内部对象**原样透传**
 * （formatter 被深拷贝会直接失效）。
 */
export function resolveCssVars<T>(value: T): T {
  if (typeof value === "string") return resolveCssVarsInString(value) as unknown as T;
  if (Array.isArray(value)) return value.map((it) => resolveCssVars(it)) as unknown as T;
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = resolveCssVars(v);
    return out as T;
  }
  return value;
}
