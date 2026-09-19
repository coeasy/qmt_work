import { describe, expect, it } from "vitest";
import { detailText } from "@/domains/research/screen/ScreenPanels";
import type { ScreenRow } from "@/services/api";

/**
 * 经典策略「命中理由」必须出现在结果里。
 *
 * 存在意义：`detailText` 此前用 `k !== "reason"` **显式跳过了 reason**，于是
 * 「命中理由」那一列只剩一串数值明细（ma20 1450.50 · vol_ratio 1.90），用户
 * 完全看不出这只票凭什么入选 —— 是创新高？均线多头？还是 RPS 达标？
 * 而 reason 恰恰是各策略返回的**唯一解释字段**（见 `screener/classic.py` 每个
 * `_st_*` 函数的返回值）。
 */
describe("detailText · 命中理由", () => {
  it("reason 排在最前面（它是「为什么选中」的唯一解释）", () => {
    const row = {
      code: "600519.SH",
      close: 1500,
      change_pct: 3.2,
      reason: "创60日新高+成交额达标+阳线",
      ma20: 1450.5,
      vol_ratio: 1.9,
    } as unknown as ScreenRow;
    const text = detailText(row);
    expect(text.startsWith("创60日新高+成交额达标+阳线")).toBe(true);
    // ≥1000 的数值走整数显示（1450.5 → 1451），避免长数字挤爆列宽
    expect(text).toContain("ma20 1451");
    expect(text).toContain("vol_ratio 1.90");
  });

  it("reason 不会作为 key value 重复出现", () => {
    const row = { code: "600519.SH", reason: "站上均线+多头+放量" } as unknown as ScreenRow;
    const text = detailText(row);
    expect(text).toBe("站上均线+多头+放量");
    expect(text).not.toContain("reason");
  });

  it("无 reason 时只渲染明细（不出现空段）", () => {
    const row = { code: "600519.SH", ma20: 12.5 } as unknown as ScreenRow;
    expect(detailText(row)).toBe("ma20 12.50");
  });

  it("reason 为空串时同样不产生空段", () => {
    const row = { code: "600519.SH", reason: "", ma20: 12.5 } as unknown as ScreenRow;
    expect(detailText(row)).toBe("ma20 12.50");
  });

  it("已单独成列的字段不重复出现在明细里", () => {
    const row = {
      code: "600519.SH", name: "贵州茅台", close: 1500, change_pct: 3.2,
      score: 0.9, strategy: "turtle_trade", volume: 100, amount: 200,
      reason: "创60日新高",
    } as unknown as ScreenRow;
    const text = detailText(row);
    expect(text).toBe("创60日新高");
  });

  it("布尔明细只显示为 true 的那些（false 不占位置）", () => {
    const row = {
      code: "600519.SH", reason: "创60日新高", is_new_high: true, yang_line: false,
    } as unknown as ScreenRow;
    const text = detailText(row);
    expect(text).toContain("is_new_high");
    expect(text).not.toContain("yang_line");
  });

  it("完全无字段时回退为 --（不是空字符串）", () => {
    expect(detailText({ code: "600519.SH" } as unknown as ScreenRow)).toBe("--");
  });
});
