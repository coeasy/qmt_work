import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { isLivePrice, staleQuoteNote } from "@/shared/freshness";

/**
 * 数据新鲜度：界面上区分「实时行情价」与「快照价」的判据。
 *
 * ★ 为什么必须测：持仓 / 模拟盘 / 汇总表在拿不到实时行情时都会**回退到快照价**，
 * 而回退后的数字和实时价长得一模一样。用户拿上一交易日的收盘价当现价去做加减仓、
 * 甚至填下单价格，金额类误判代价很高。这里锁住两件事：
 *   ① 「价格可用」的判据（0 与缺失都算不可用）；
 *   ② 这条规则**只有一个实现**，不许在各页面里各写一遍（写了就会漂移）。
 */

const SRC = resolve(__dirname, "../src");

function collect(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) collect(p, out);
    else if (/\.tsx?$/.test(p)) out.push(p);
  }
  return out;
}

describe("isLivePrice", () => {
  it("正常价格可用", () => {
    expect(isLivePrice(12.34)).toBe(true);
  });

  it("价格 0 视为不可用 —— 停牌会推 0，不能当成真价格", () => {
    expect(isLivePrice(0)).toBe(false);
  });

  it("缺失（undefined / null）不可用", () => {
    expect(isLivePrice(undefined)).toBe(false);
    expect(isLivePrice(null)).toBe(false);
  });

  it("负数与 NaN 不可用", () => {
    expect(isLivePrice(-1)).toBe(false);
    expect(isLivePrice(NaN)).toBe(false);
  });
});

describe("staleQuoteNote", () => {
  it("给出条数与总条数", () => {
    const t = staleQuoteNote(2, 5, "尾部说明。");
    expect(t).toContain("有 2 / 5 条未取到实时行情");
    expect(t).toContain("尾部说明。");
  });

  it("有快照时间时带上「生成于」", () => {
    const t = staleQuoteNote(1, 3, "尾部。", "2026-09-18 15:00:00");
    expect(t).toContain("（生成于 2026-09-18 15:00:00）");
  });

  it("没有快照时间时不编造时间", () => {
    expect(staleQuoteNote(1, 3, "尾部。")).not.toContain("生成于");
  });
});

describe("新鲜度判据不许在各页面各写一遍", () => {
  /**
   * 规则此前在 AssetSummary / Positions / Paper / Trade / MarketStructure 等处
   * 以 `x !== undefined && x > 0` 的形式各写了一遍 —— 改一处漏一处就会出现
   * 「同一张表有的行标了有的没标」。这里做源码扫描，把回潮挡在门禁里
   * （与 `moduleResolution.test.ts` 同手法：tsc 查不出这类语义漂移）。
   */
  const INLINE = [
    /\w+\s*!==\s*undefined\s*&&\s*\w+\s*>\s*0/,
    /\w+\s*>\s*0\s*&&\s*\w+\s*!==\s*undefined/,
    /typeof\s+\w+\s*===\s*["']number["']\s*&&\s*\w+\s*>\s*0/,
  ];

  it("src 下不存在内联的「价格可用」判定", () => {
    const offenders: string[] = [];
    for (const f of collect(SRC)) {
      const rel = relative(SRC, f).replace(/\\/g, "/");
      // 唯一实现就在这里
      if (rel === "shared/freshness.ts") continue;
      const src = readFileSync(f, "utf8");
      // 只看带 price / last / close 语义的行，避免误伤与价格无关的数值判断
      for (const line of src.split("\n")) {
        if (!/price|last|close|Price|Last|Close/.test(line)) continue;
        if (INLINE.some((re) => re.test(line))) {
          offenders.push(`${rel}: ${line.trim()}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("快照价的标注必须带解释", () => {
  it("用到 .stalePrice 标记的文件，都在同一处给了「非实时」悬浮说明", () => {
    const bad: string[] = [];
    for (const f of collect(SRC)) {
      const src = readFileSync(f, "utf8");
      if (!/\bstalePrice\b/.test(src)) continue;
      const rel = relative(SRC, f).replace(/\\/g, "/");
      // 只标样式、不给原因 ⇒ 用户看到一个下划线却不知道意味着什么
      if (!src.includes("非实时")) bad.push(rel);
    }
    expect(bad).toEqual([]);
  });

  it(".stalePrice 样式确实存在（样式丢了标记会静默失效）", () => {
    const css = readFileSync(join(SRC, "domains/domain.module.css"), "utf8");
    expect(css).toMatch(/\.stalePrice\s*\{/);
  });
});
