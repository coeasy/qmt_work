import { describe, expect, it } from "vitest";
import {
  CUSTOM_SKIN_ID,
  PRESETS,
  SKIN_TOKEN_KEYS,
  deriveSkinTokens,
  isDarkColor,
} from "@/design/skins";
import { resolveSkin } from "@/stores/ui";

/**
 * 背景配色（皮肤）回归测试。
 *
 * 这里锁死的是**可读性不变量**，不是具体色值：
 * 用户可以选任意背景色，但无论选什么，文字与背景的亮度关系必须始终正确 ——
 * 否则会出现「白底白字」这种「功能没坏但界面废了」的故障，且只有当事人能发现。
 */

/** 从 `hsl(H S% L%)` 里取亮度（0-100） */
function lightness(token: string | undefined): number {
  const m = /hsl\(\s*\d+\s+(\d+)%\s+(\d+)%\s*\)/.exec(token ?? "");
  return m ? Number(m[2]) : NaN;
}

describe("自定义背景色 → 成套令牌", () => {
  const CASES = [
    "#000000", // 纯黑（同花顺经典）
    "#1c1c1c", // 深灰（大智慧）
    "#0d1117", // 午夜蓝
    "#101a15", // 墨绿
    "#7a2f2f", // 深红（饱和度高）
    "#c7edcc", // 护眼浅绿
    "#ffffff", // 纯白
    "#f2f4f8", // 浅灰
    "#ff0000", // 极端饱和
  ];

  it("对任意合法输入都给出完整令牌集", () => {
    for (const bg of CASES) {
      const t = deriveSkinTokens(bg);
      expect(t, bg).not.toBeNull();
      for (const k of SKIN_TOKEN_KEYS) {
        expect(t?.[k], `${bg} 缺少 ${k}`).toBeTruthy();
      }
    }
  });

  it("★不变量：深色底 → 文字比背景亮；浅色底 → 文字比背景暗", () => {
    for (const bg of CASES) {
      const t = deriveSkinTokens(bg);
      if (!t) throw new Error(`解析失败：${bg}`);
      const bgL = lightness(t["--bg-1"]);
      const textL = lightness(t["--text"]);
      expect(Number.isNaN(bgL), bg).toBe(false);
      expect(Number.isNaN(textL), bg).toBe(false);
      if (isDarkColor(bg)) {
        expect(textL, `${bg}: 深底应配浅字`).toBeGreaterThan(bgL + 25);
      } else {
        expect(textL, `${bg}: 浅底应配深字`).toBeLessThan(bgL - 25);
      }
    }
  });

  it("★不变量：层级单调（bg-0 → bg-3 逐级远离底色）", () => {
    for (const bg of CASES) {
      const t = deriveSkinTokens(bg);
      if (!t) throw new Error(`解析失败：${bg}`);
      const seq = ["--bg-1", "--bg-2", "--bg-3"].map((k) => lightness(t[k]));
      const dark = isDarkColor(bg);
      for (let i = 1; i < seq.length; i += 1) {
        if (dark) expect(seq[i], `${bg} 层级应逐级变亮`).toBeGreaterThan(seq[i - 1] ?? -1);
        else expect(seq[i], `${bg} 层级应逐级变暗`).toBeLessThan(seq[i - 1] ?? 101);
      }
    }
  });

  it("背景饱和度被压到 35% 以内（底色不能太花，否则淹没涨跌色）", () => {
    for (const bg of ["#ff0000", "#00ff00", "#0000ff"]) {
      const t = deriveSkinTokens(bg);
      const m = /hsl\(\s*\d+\s+(\d+)%/.exec(t?.["--bg-1"] ?? "");
      expect(Number(m?.[1]), bg).toBeLessThanOrEqual(35);
    }
  });

  it("支持 #abc 短格式，大小写均可", () => {
    expect(deriveSkinTokens("#abc")).not.toBeNull();
    expect(deriveSkinTokens("#ABC")).not.toBeNull();
    expect(deriveSkinTokens("#0d1117")).toEqual(deriveSkinTokens("0d1117"));
  });

  it("非法输入返回 null（调用方保持当前皮肤不动）", () => {
    for (const bad of ["", "  ", "red", "#12345", "#gggggg", "hsl(0 0% 0%)"]) {
      expect(deriveSkinTokens(bad), bad).toBeNull();
    }
  });
});

describe("皮肤与明暗方向的匹配", () => {
  it("★不变量：错配（深色皮肤 + 浅色主题）必须退回主题默认，不写内联令牌", () => {
    // 经典黑是深色皮肤，配浅色主题 → 不生效
    const mismatched = resolveSkin("classic", "#000000", "light");
    expect(mismatched.active).toBe("");
    expect(mismatched.tokens).toBeNull();

    // 同一皮肤配深色主题 → 生效（预设走 CSS，不需要内联令牌）
    const matched = resolveSkin("classic", "#000000", "dark");
    expect(matched.active).toBe("classic");
    expect(matched.tokens).toBeNull();
  });

  it("自定义皮肤的明暗由背景色决定", () => {
    expect(resolveSkin(CUSTOM_SKIN_ID, "#101820", "dark").active).toBe(CUSTOM_SKIN_ID);
    expect(resolveSkin(CUSTOM_SKIN_ID, "#101820", "light").active).toBe("");
    expect(resolveSkin(CUSTOM_SKIN_ID, "#f2f4f8", "light").active).toBe(CUSTOM_SKIN_ID);
    expect(resolveSkin(CUSTOM_SKIN_ID, "#f2f4f8", "dark").active).toBe("");
  });

  it("未知皮肤 id 不生效（不抛错）", () => {
    expect(resolveSkin("does-not-exist", "#000000", "dark").active).toBe("");
    expect(resolveSkin("", "#000000", "dark").active).toBe("");
  });
});

describe("预设皮肤自洽性", () => {
  it("每个预设的 tone 与主色明暗一致（否则选中后主题方向会反）", () => {
    for (const p of PRESETS) {
      expect(isDarkColor(p.swatch), `${p.id} 主色与 tone 不一致`).toBe(p.tone === "dark");
    }
  });

  it("预设 id 唯一", () => {
    const ids = PRESETS.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("提供同花顺 / 大智慧风格的经典底色", () => {
    const ids = PRESETS.map((p) => p.id);
    expect(ids).toContain("classic"); // 同花顺：经典黑
    expect(ids).toContain("graphite"); // 大智慧：深灰
    expect(ids).toContain("midnight"); // 默认
  });
});
