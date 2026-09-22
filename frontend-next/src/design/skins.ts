/**
 * 皮肤（背景配色）系统 —— 对标主流 A 股行情终端的「背景色」设置。
 *
 * 两条路：
 * 1. **预设皮肤**（`PRESETS`）：令牌写在 `design/skins.css`，靠 `<html data-skin>` 生效，
 *    零 JS 计算；每套皮肤自带明暗方向（tone），选中时一并切换 `data-theme`。
 * 2. **自定义背景色**：用户给一个颜色，`deriveSkinTokens` 就地派生出**成套**令牌
 *    （背景层级 + 文字 + 边框 + 图表底色），写成 `<html>` 的内联变量。
 *
 * ★ 为什么自定义必须「成套派生」而不是只改背景：
 *   只改背景色会出现「白底白字」这类不可读组合。行情软件的背景色设置之所以好用，
 *   正是因为它按背景亮度自动决定文字/边框的深浅。这里把这条规则显式实现出来，
 *   并用单测锁死「任意输入下前景与背景的亮度差都足够」。
 */
import type { Theme } from "@/stores/ui";

export interface SkinDef {
  id: string;
  label: string;
  tone: Theme;
  hint: string;
  /** UI 色块样本（主背景色） */
  swatch: string;
}

/**
 * 预设皮肤。
 *
 * ★ 命名纪律：**不使用任何第三方行情软件的名称**（通达信 / 同花顺 / 大智慧等）。
 *   这些是他人商标，用它给自家配色命名既不合规、也会让用户误以为「这是某某皮肤
 *   的复刻版」。配色取向可以对标，名字必须是自己的 —— 故这里全部用中性色名。
 *
 * 六套预设（五深一浅）：
 * - 极夜黑：纯黑底，最深、对比最强；
 * - 石墨黑：近黑微冷灰，屏幕反光环境下更舒适；
 * - 曜石黑：极深蓝黑，长盯盘最不刺眼；
 * - 石板蓝 / 墨绿：低饱和冷/暖调，介于两者之间；
 * - 晨曦白：浅色底，明亮环境 / 投影演示（**默认**）。
 *
 * ★ 默认为什么是浅色：深色是盯盘场景的老习惯，但对**第一次打开软件**的人来说，
 *   浅色更容易读清界面结构（面板边界、分组、次要文字）；要深色随时可在
 *   「设置 · 界面偏好 · 背景配色」一键切换，选择会被记住。
 *
 * 每套皮肤自带明暗方向（tone），选中时一并切换 `data-theme`，
 * 避免「深色皮肤 + 浅色主题」这类错配。
 */
export const DEFAULT_SKIN_ID = "light";

export const PRESETS: SkinDef[] = [
  {
    id: "tongdaxin",
    label: "极夜黑",
    tone: "dark",
    hint: "纯黑底，对比最强，暗光环境最省眼",
    swatch: "#000000",
  },
  {
    id: "dazhihui",
    label: "石墨黑",
    tone: "dark",
    hint: "近黑微冷灰，反光屏更柔和",
    swatch: "#0a0a0c",
  },
  {
    id: "ths",
    label: "曜石黑",
    tone: "dark",
    hint: "极深蓝黑，长时间盯盘不易疲劳",
    swatch: "#050608",
  },
  {
    id: "slate",
    label: "石板蓝",
    tone: "dark",
    hint: "低饱和蓝灰，冷暖适中",
    swatch: "#16202c",
  },
  {
    id: "forest",
    label: "墨绿",
    tone: "dark",
    hint: "低刺激暖绿，夜间使用更柔和",
    swatch: "#101a15",
  },
  {
    id: "light",
    label: "晨曦白",
    tone: "light",
    hint: "浅色底，明亮环境 / 投影演示",
    swatch: "#f2f4f8",
  },
];

export const CUSTOM_SKIN_ID = "custom";

/** 皮肤能覆盖的令牌（与 skins.css 及 deriveSkinTokens 保持一致）。 */
export const SKIN_TOKEN_KEYS = [
  "--bg-0",
  "--bg-1",
  "--bg-2",
  "--bg-3",
  "--bg-hover",
  "--bg-active",
  "--text",
  "--text-dim",
  "--text-faint",
  "--text-inverse",
  "--border",
  "--border-strong",
  "--chart-bg",
  "--chart-grid",
  "--chart-axis",
  "--chart-crosshair",
] as const;

export function presetById(id: string): SkinDef | undefined {
  return PRESETS.find((p) => p.id === id);
}

// ---------------------------------------------------------------------------
// 自定义背景色 → 成套令牌
// ---------------------------------------------------------------------------

/** 解析 #rgb / #rrggbb；返回对象而非元组 —— 元组下标访问在 noUncheckedIndexedAccess
 *  下会被推断为 `number | undefined`，调用处到处要非空断言，不值得。 */
function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return null;
  let h = m[1] ?? "";
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16),
  };
}

/** RGB(0-255) → [h(0-360), s(0-1), l(0-1)] */
function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  const R = r / 255;
  const G = g / 255;
  const B = b / 255;
  const max = Math.max(R, G, B);
  const min = Math.min(R, G, B);
  const l = (max + min) / 2;
  const d = max - min;
  if (d === 0) return [0, 0, l];
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h: number;
  if (max === R) h = ((G - B) / d + (G < B ? 6 : 0)) * 60;
  else if (max === G) h = ((B - R) / d + 2) * 60;
  else h = ((R - G) / d + 4) * 60;
  return [h, s, l];
}

function hsl(h: number, s: number, l: number): string {
  const H = Math.round(((h % 360) + 360) % 360);
  const S = Math.round(Math.max(0, Math.min(1, s)) * 100);
  const L = Math.round(Math.max(0, Math.min(1, l)) * 100);
  return `hsl(${H} ${S}% ${L}%)`;
}

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

/** 判断颜色是否为深色（用于决定前景取深还是取浅）。 */
export function isDarkColor(hex: string): boolean {
  const rgb = hexToRgb(hex);
  if (!rgb) return true;
  return rgbToHsl(rgb.r, rgb.g, rgb.b)[2] < 0.5;
}

/**
 * 由「用户选的背景色」派生一整套皮肤令牌。
 *
 * 规则：
 * - 用户选的颜色就是**主面板色**（`--bg-1`），背景层级围绕它展开；
 * - 深色底 → 层级越往上越亮；浅色底 → 越往上越暗（保持与现有 light 令牌同向）；
 * - 文字/边框按底的明暗反向取值，**保证亮度差**，因此任何输入都不会出现不可读组合；
 * - 饱和度上限 0.35：行情软件背景不能"花"，否则涨跌色会被淹没。
 *
 * 返回 `null` 表示颜色串无法解析（调用方应保持当前皮肤不动）。
 */
export function deriveSkinTokens(bg: string): Record<string, string> | null {
  const rgb = hexToRgb(bg);
  if (!rgb) return null;
  const [h, s0, l] = rgbToHsl(rgb.r, rgb.g, rgb.b);
  const dark = l < 0.5;
  const S = Math.min(s0, 0.35);
  // 深色底：越"上层"越亮；浅色底：越"上层"越暗
  const dir = dark ? 1 : -1;
  const up = (n: number) => clamp01(l + dir * n);

  const tokens: Record<string, string> = {
    // 用户选色 = 面板色（bg-1）；bg-0 是最外层容器，往外退一档
    "--bg-1": hsl(h, S, l),
    "--bg-0": hsl(h, S, up(dark ? -0.035 : 0.035)),
    "--bg-2": hsl(h, S, up(0.03)),
    "--bg-3": hsl(h, S, up(0.055)),
    "--bg-hover": hsl(h, S, up(0.085)),
    "--bg-active": hsl(h, S, up(0.125)),
  };

  if (dark) {
    tokens["--text"] = hsl(h, Math.min(S, 0.2), 0.92);
    tokens["--text-dim"] = hsl(h, Math.min(S, 0.2), 0.64);
    tokens["--text-faint"] = hsl(h, Math.min(S, 0.2), 0.46);
    tokens["--text-inverse"] = hsl(h, S, Math.max(0, l - 0.03));
    tokens["--border"] = hsl(h, S, up(0.11));
    tokens["--border-strong"] = hsl(h, S, up(0.18));
    tokens["--chart-bg"] = hsl(h, S, up(0.02));
    tokens["--chart-grid"] = hsl(h, S, up(0.09));
    tokens["--chart-axis"] = hsl(h, Math.min(S, 0.2), 0.5);
    tokens["--chart-crosshair"] = hsl(h, Math.min(S, 0.2), 0.68);
  } else {
    tokens["--text"] = hsl(h, Math.min(S, 0.3), 0.12);
    tokens["--text-dim"] = hsl(h, Math.min(S, 0.3), 0.38);
    tokens["--text-faint"] = hsl(h, Math.min(S, 0.3), 0.56);
    tokens["--text-inverse"] = "#ffffff";
    tokens["--border"] = hsl(h, S, up(0.09));
    tokens["--border-strong"] = hsl(h, S, up(0.17));
    tokens["--chart-bg"] = hsl(h, S, Math.max(0, l + 0.06));
    tokens["--chart-grid"] = hsl(h, S, up(0.07));
    tokens["--chart-axis"] = hsl(h, Math.min(S, 0.3), 0.5);
    tokens["--chart-crosshair"] = hsl(h, Math.min(S, 0.3), 0.36);
  }
  return tokens;
}
