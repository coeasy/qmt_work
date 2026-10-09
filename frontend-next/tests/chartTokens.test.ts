import { describe, expect, it, beforeEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { resolveCssVars, resolveCssVarsInString, cssVar } from "@/charts/cssVar";

/**
 * 图表配色「令牌 → 真值」的回归锁 —— 2026-10-03。
 *
 * ## 这条缺陷为什么长期没人发现
 * ECharts 走 **CanvasRenderer**，而 Canvas **不解析 CSS 自定义属性**：把
 * `var(--up)` 写进 option，zrender 的颜色解析器拿到 `undefined`，图表照画、
 * 颜色却是兜底色 —— **不报错、不告警**，于是
 *   - 涨跌色切到「红涨蓝跌」后，K 线（klinecharts，用 getComputedStyle 取真值）
 *     变蓝了，而分时 / 资金流 / 市场结构 / 板块雷达（ECharts）**纹丝不动**；
 *   - 浅色主题下这些图表的坐标轴、网格也拿不到正确色值。
 *
 * 修法集中在 `charts/cssVar.ts` + `charts/EChart.tsx`（setOption 前统一解析），
 * 调用方照旧写 `var(--x)`。本文件锁两件事：
 *   ① 解析函数本身（令牌命中 / 兜底 / 嵌套结构 / **函数 formatter 必须原样透传**）；
 *   ② 源码级：图表与行情组件**不得**再出现硬编码涨跌色（把口径钉死在代码里）。
 */

const SRC = path.resolve(__dirname, "../src");
const read = (rel: string) => fs.readFileSync(path.join(SRC, rel), "utf8");
/** 剥注释后再扫：注释里会**引用**旧色值说明「不要这么写」，全文匹配会误判 */
const stripComments = (t: string) =>
  t.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/[^\n]*/g, "$1");

describe("cssVar · 令牌解析", () => {
  beforeEach(() => {
    document.documentElement.style.setProperty("--up", "#ff0000");
    document.documentElement.style.setProperty("--down", "#0000ff");
  });

  it("字符串里的 var(--x) 被换成真值", () => {
    expect(cssVar("--up")).toBe("#ff0000");
    expect(resolveCssVarsInString("var(--up)")).toBe("#ff0000");
    expect(resolveCssVarsInString("stroke:var(--down);")).toBe("stroke:#0000ff;");
  });

  it("令牌取不到时用 var() 自带的兜底，绝不留原串", () => {
    expect(resolveCssVarsInString("var(--nope, #123456)")).toBe("#123456");
    expect(cssVar("--nope", "#abcdef")).toBe("#abcdef");
  });

  it("嵌套 option（对象 / 数组）递归解析", () => {
    const out = resolveCssVars({
      series: [{ lineStyle: { color: "var(--up)" } }, { itemStyle: { color: "var(--down)" } }],
      xAxis: { axisLine: { lineStyle: { color: "var(--chart-grid, #222)" } } },
    }) as any;
    expect(out.series[0].lineStyle.color).toBe("#ff0000");
    expect(out.series[1].itemStyle.color).toBe("#0000ff");
    expect(out.xAxis.axisLine.lineStyle.color).toBe("#222");
  });

  it("★ 函数 formatter 必须原样透传（深拷贝会把它变成空对象）", () => {
    const fn = (v: number) => `${v}`;
    const out = resolveCssVars({ tooltip: { formatter: fn }, color: "var(--up)" }) as any;
    expect(out.tooltip.formatter).toBe(fn);
    expect(out.color).toBe("#ff0000");
  });
});

describe("源码级：图表不得硬编码涨跌色", () => {
  /**
   * 允许硬编码的文件（都是**颜色定义本身**或 `v(令牌, 兜底)` 的第二参数）：
   * - `design/tokens.css` / `design/skins.ts`：调色板定义
   * - `stores/ui.ts`：自定义背景色默认值
   * - `domains/system/Settings.tsx`：强调色取色器（UI 本身要显示一个色值）
   * - `charts/KLineChart.tsx`：`v("--up", "#ef4444")` 的**兜底参数**
   */
  const ALLOW = new Set([
    "design/tokens.css",
    "design/skins.ts",
    "stores/ui.ts",
    "domains/system/Settings.tsx",
    "charts/KLineChart.tsx",
  ]);

  const files: string[] = [];
  (function walk(dir: string, rel = "") {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) walk(path.join(dir, e.name), r);
      else if (/\.(ts|tsx)$/.test(e.name) && !ALLOW.has(r)) files.push(r);
    }
  })(SRC);

  it("除调色板定义与 K 线兜底参数外，源码里不得出现硬编码色值", () => {
    const hits: string[] = [];
    for (const f of files) {
      const text = stripComments(read(f));
      text.split("\n").forEach((line, i) => {
        // rgba(...) / 渐变等不算「硬编码令牌色」，只拦 6 位 hex
        if (/#[0-9a-fA-F]{6}\b/.test(line)) hits.push(`${f}:${i + 1} ${line.trim()}`);
      });
    }
    // ★ 曾经的真实违规：MinutesChart 写死 #ef4444/#22c55e（分时跌线永远是绿的，
    //   与改成红涨蓝跌后的 K 线在同一屏打架）；SectorRadar 热力图写死绿→红。
    expect(hits, `发现硬编码色值（应改用 var(--x) 令牌）：\n${hits.join("\n")}`).toEqual([]);
  });

  it("★ 引用的 var(--x) 必须是**真实存在**的令牌（2026-10-09 新增）", () => {
    // 背景：R27/R28 新增页面里写了 ``var(--color-danger,#c0392b)`` 这类
    // **编造的令牌名** —— 它同时犯了两个错：① 令牌根本不存在（真实名是
    // ``--danger``），解析出来是透明/继承色，界面上「红色警告」直接消失；
    // ② 第二参数的硬编码 hex 又撞上上面的「禁止硬编码色值」门禁。
    // 只查 hex 抓不到①，只查空值又抓不到②，所以两条都要。
    const cssDir = path.join(SRC, "design");
    const defined = new Set<string>();
    for (const f of fs.readdirSync(cssDir).filter((n) => n.endsWith(".css"))) {
      const txt = fs.readFileSync(path.join(cssDir, f), "utf8");
      for (const m of txt.matchAll(/(--[A-Za-z0-9_-]+)\s*:/g)) defined.add(m[1] ?? "");
    }
    // charts/cssVar.ts 与 skins.ts 里可能以 JS 侧名字声明的令牌
    for (const rel of ["charts/cssVar.ts", "design/skins.ts"]) {
      const p = path.join(SRC, rel);
      if (!fs.existsSync(p)) continue;
      for (const m of fs.readFileSync(p, "utf8").matchAll(/["'`](--[A-Za-z0-9_-]+)["'`]/g)) {
        defined.add(m[1] ?? "");
      }
    }
    expect(defined.size, "令牌集为空 ⇒ 解析路径不对").toBeGreaterThan(20);

    const unknown: string[] = [];
    for (const f of files) {
      const text = stripComments(read(f));
      text.split("\n").forEach((line, i) => {
        for (const m of line.matchAll(/var\(\s*(--[A-Za-z0-9_-]+)/g)) {
          const tok = m[1] ?? "";
          if (!defined.has(tok)) unknown.push(`${f}:${i + 1} ${tok}`);
        }
      });
    }
    // 去重：同一文件同一令牌只报一次，避免输出爆炸
    expect([...new Set(unknown)],
      `引用了不存在的 CSS 令牌（真实令牌见 design/tokens.css）：`).toEqual([]);
  });

  it("★ 分时图与板块雷达必须走涨跌令牌（本轮修掉的两处）", () => {
    const m = stripComments(read("domains/market/panels/MinutesChart.tsx"));
    expect(m).toContain("var(--up)");
    expect(m).toContain("var(--down)");
    expect(m).not.toContain("#ef4444");
    expect(m).not.toContain("#22c55e");

    const r = stripComments(read("domains/market/SectorRadar.tsx"));
    expect(r).toContain("var(--up)");
    expect(r).toContain("var(--down)");
  });

  it("★ EChart 必须订阅 theme/updown 并重解析（否则换配色画布不跟着变）", () => {
    const src = stripComments(read("charts/EChart.tsx"));
    expect(src, "EChart 未订阅主题/涨跌色").toContain("useUiStore");
    expect(src, "setOption 前未解析 CSS 变量（canvas 不认 var()）").toContain("resolveCssVars");
    // 依赖里必须带上这两个值，否则 option 引用不变 ⇒ 永远不重绘
    expect(src).toMatch(/\[option,\s*notMerge,\s*uiTheme,\s*updown\]/);
  });
});
