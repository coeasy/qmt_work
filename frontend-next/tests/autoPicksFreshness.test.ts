import { describe, expect, it } from "vitest";
import { isBarDateLagging } from "@/domains/research/screen/AutoPicks";

/**
 * 「自动选股」页的数据新鲜度判据回归测试（2026-09-20 修复）。
 *
 * 修的是什么：元数据行内提示原先写的是
 *   `run.bar_date < new Date().toISOString().slice(0,10).replace(/-/g,"")`
 * 两个错：
 *   ① 拿「今天」当判据 ⇒ **每逢周末/节假日必然误报**「行情未更新到最新交易日」
 *      （bar_date = 周五，今天 = 周日），而数据其实是最新的；
 *   ② `toISOString()` 是 **UTC** 的今天 ⇒ UTC+8 用户在早上 8 点前会拿到**前一天**，
 *      判据再错一天。
 *
 * 现在的判据是「落后于最近交易日」——``expectDate`` 由后端 `GET /market/session`
 * 给出（交易日 = 今天，非交易日 = 最近交易日），与页面级 `lagging` 同一口径。
 */
describe("isBarDateLagging", () => {
  it("周末：bar_date 等于最近交易日 ⇒ 不算落后（原先必误报）", () => {
    // 2026-09-20 是周日，最近交易日是周五 20260918
    expect(isBarDateLagging("20260918", "20260918")).toBe(false);
  });

  it("交易日：bar_date 就是今天 ⇒ 不算落后", () => {
    expect(isBarDateLagging("20260920", "20260920")).toBe(false);
  });

  it("确实落后：bar_date 早于最近交易日 ⇒ 落后", () => {
    expect(isBarDateLagging("20260911", "20260918")).toBe(true);
    expect(isBarDateLagging("20250418", "20260918")).toBe(true);
  });

  it("bar_date 为空 ⇒ 交给「未取到数据」分支，不在此处报落后", () => {
    expect(isBarDateLagging("", "20260918")).toBe(false);
  });

  it("expectDate 未知（会话快照没取到）⇒ 不判落后，绝不凭空断言", () => {
    expect(isBarDateLagging("20260918", "")).toBe(false);
    expect(isBarDateLagging("", "")).toBe(false);
  });

  it("跨年边界：字符串比较与日期序一致（YYYYMMDD 定长）", () => {
    expect(isBarDateLagging("20251231", "20260101")).toBe(true);
    expect(isBarDateLagging("20260101", "20251231")).toBe(false);
  });

  it("绝不使用 UTC 的「今天」：同一 bar_date 在周日与周一结论一致", () => {
    // 若判据写成「< 今天」，周日的 expectDate 会是 20260920（今天），
    // 于是 20260918 < 20260920 ⇒ 误报落后。这里锁住「以最近交易日为准」。
    expect(isBarDateLagging("20260918", "20260918")).toBe(false);
  });
});
