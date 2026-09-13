/**
 * 时间解析工具。
 *
 * ★ 为什么需要这个文件：后端 K 线 `time` 字段**不是一个统一格式**。
 *   实测各数据源输出至少四种（均来自 datasource/*.py）：
 *
 *   | 数据源                       | 代码位置                    | 输出示例              |
 *   |------------------------------|-----------------------------|-----------------------|
 *   | eltdx（TDX 补充源）           | eltdx_source.py:435/752     | `20260817`（%Y%m%d）  |
 *   | local_store（SQLite 缓存）    | local_store.py:75           | `20260817`（沿用上游）|
 *   | akshare                      | akshare_source.py:123       | `2026-08-17`          |
 *   | optional_sources（baostock 等）| optional_sources.py:56/139  | `2026-08-17 00:00:00` |
 *   | public_sources               | public_sources.py:64/94     | `2026-08-17`          |
 *
 *   早期实现只认 `YYYY-MM-DD`，遇到 eltdx 的 `20260817` 会构造出
 *   `new Date("20260817T00:00:00")` → Invalid Date → 落到 `Date.now()` 兜底，
 *   结果是**所有 K 线共用同一个「当前时间」时间戳**：X 轴标签全部显示同一天，
 *   十字光标与分页过滤（timestamp < timestamp）一并失效。故此处按
 *   「位数 + 分隔符」分支解析，覆盖全部已知格式。
 *
 * ★ 一律用「本地时间」构造，禁止 `new Date("2026-08-17")`：
 *   ECMAScript 规定「只有日期、没有时间」的字符串按 **UTC** 解析，
 *   在 GMT+8 会被回退到前一天 08:00，导致日线整体错位一天。
 */

/** 纯数字分支：YYYYMMDD / YYYYMMDDHHMMSS / epoch 秒 / epoch 毫秒 */
const DIGITS = /^\d+$/;
/** 带分隔符分支：YYYY-MM-DD 或 YYYY/MM/DD，可带时间 */
const SEPARATED = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?/;

/**
 * 把后端 K 线时间串解析为毫秒时间戳（本地时区）。
 *
 * 无法识别时返回 `NaN`——**不再兜底为 `Date.now()`**：
 * 兜底会让所有异常值堆在同一时刻，产生「看起来正常、其实全错」的图表，
 * 比直接暴露问题危险得多。调用方应过滤掉 `NaN`（见 toKLineData）。
 */
export function toTimestamp(time: string | null | undefined): number {
  if (!time) return NaN;
  const s = String(time).trim();
  if (!s) return NaN;

  if (DIGITS.test(s)) {
    if (s.length === 8) {
      // YYYYMMDD（eltdx / local_store 日线）
      return new Date(
        Number(s.slice(0, 4)),
        Number(s.slice(4, 6)) - 1,
        Number(s.slice(6, 8)),
      ).getTime();
    }
    if (s.length === 14) {
      // YYYYMMDDHHMMSS（eltdx 分钟线）
      return new Date(
        Number(s.slice(0, 4)),
        Number(s.slice(4, 6)) - 1,
        Number(s.slice(6, 8)),
        Number(s.slice(8, 10)),
        Number(s.slice(10, 12)),
        Number(s.slice(12, 14)),
      ).getTime();
    }
    const n = Number(s);
    if (!Number.isFinite(n)) return NaN;
    if (s.length === 10) return n * 1000; // epoch 秒
    if (s.length === 13) return n; // epoch 毫秒
    return n; // 其它长度：按毫秒原样使用
  }

  const m = SEPARATED.exec(s);
  if (m) {
    const y = Number(m[1] ?? 0);
    const mo = Number(m[2] ?? 1);
    const d = Number(m[3] ?? 1);
    const h = Number(m[4] ?? 0);
    const mi = Number(m[5] ?? 0);
    const sec = Number(m[6] ?? 0);
    const t = new Date(y, mo - 1, d, h, mi, sec).getTime();
    return Number.isFinite(t) ? t : NaN;
  }

  // 兜底：ISO 8601 等带时区信息的字符串
  const t = new Date(s).getTime();
  return Number.isFinite(t) ? t : NaN;
}

/** 毫秒时间戳 → `YYYY-MM-DD`（本地时区），用于表格/十字光标展示 */
export function fmtDate(ts: number): string {
  if (!Number.isFinite(ts)) return "--";
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 毫秒时间戳 → `YYYY-MM-DD HH:MM`（本地时区） */
export function fmtDateTime(ts: number): string {
  if (!Number.isFinite(ts)) return "--";
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${fmtDate(ts)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
