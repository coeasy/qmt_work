import { describe, expect, it } from "vitest";
import {
  fmtAmount,
  fmtPct,
  fmtPrice,
  fmtSigned,
  fmtVolume,
  isTradingHours,
  normalizeCode,
  tone,
  toneColor,
} from "@/shared/format";

describe("格式化工具", () => {
  it("空值统一显示 -- 而非 0 或 NaN", () => {
    expect(fmtPrice(null)).toBe("--");
    expect(fmtPrice(undefined)).toBe("--");
    expect(fmtPrice(Number.NaN)).toBe("--");
    expect(fmtPct(null)).toBe("--");
    expect(fmtVolume(null)).toBe("--");
  });

  it("价格保留两位小数", () => {
    expect(fmtPrice(1700)).toBe("1700.00");
    expect(fmtPrice(10.005)).toBe("10.01");
  });

  it("百分比带正负号", () => {
    expect(fmtPct(3.456)).toBe("+3.46%");
    expect(fmtPct(-1.2)).toBe("-1.20%");
    expect(fmtPct(0)).toBe("+0.00%");
  });

  it("带符号数值", () => {
    expect(fmtSigned(1.5)).toBe("+1.50");
    expect(fmtSigned(-1.5)).toBe("-1.50");
  });

  it("金额与成交量按万/亿缩写", () => {
    expect(fmtAmount(1234)).toBe("1234");
    expect(fmtAmount(12345)).toBe("1.23万");
    expect(fmtAmount(123456789)).toBe("1.23亿");
    expect(fmtVolume(25000)).toBe("2.50万");
  });

  it("涨跌方向判定", () => {
    expect(tone(1)).toBe(1);
    expect(tone(-1)).toBe(-1);
    expect(tone(0)).toBe(0);
    expect(tone(null)).toBe(0);
    expect(tone(Number.NaN)).toBe(0);
  });

  it("涨跌色走 CSS 变量，不硬编码色值", () => {
    expect(toneColor(1)).toBe("var(--up)");
    expect(toneColor(-1)).toBe("var(--down)");
    expect(toneColor(0)).toBe("var(--flat)");
  });

  it("交易时段判断（A 股 09:30-11:30 / 13:00-15:00，周末休市）", () => {
    // 2026-09-14 是周一
    expect(isTradingHours(new Date("2026-09-14T10:00:00"))).toBe(true);
    expect(isTradingHours(new Date("2026-09-14T12:00:00"))).toBe(false);
    expect(isTradingHours(new Date("2026-09-14T14:00:00"))).toBe(true);
    expect(isTradingHours(new Date("2026-09-14T16:00:00"))).toBe(false);
    // 2026-09-13 是周日
    expect(isTradingHours(new Date("2026-09-13T10:00:00"))).toBe(false);
  });
});

describe("代码规范化", () => {
  it("6 开头补 SH", () => {
    expect(normalizeCode("600519")).toBe("600519.SH");
  });

  it("0/3 开头补 SZ", () => {
    expect(normalizeCode("000001")).toBe("000001.SZ");
    expect(normalizeCode("300750")).toBe("300750.SZ");
  });

  it("4/8 开头补 BJ", () => {
    expect(normalizeCode("830799")).toBe("830799.BJ");
  });

  it("已带后缀则保持不变（大小写归一）", () => {
    expect(normalizeCode("600519.sh")).toBe("600519.SH");
    expect(normalizeCode("000001.SZ")).toBe("000001.SZ");
  });

  it("无法识别的输入原样返回（不猜测）", () => {
    expect(normalizeCode("ABC")).toBe("ABC");
  });
});
