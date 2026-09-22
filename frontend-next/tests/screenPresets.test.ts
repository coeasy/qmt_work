import { describe, expect, it } from "vitest";
import { PRESET_EXAMPLES } from "@/domains/research/screen/ScreenPanels";

/**
 * 条件选股预设**不得是空条件**。
 *
 * 存在意义（2026-09-22 实测到的真缺陷）：
 * 预设「放量（量 > 5 日均量 1.5 倍）且收阳」曾被写成
 *
 *     and: [ {indicator:{name:"volume_ma", win:5, op:"gt", value:0}},
 *            {field:{name:"close",      op:"gt", value:0}} ]
 *
 * 成交量均线**恒为正**、收盘价**恒为正** ⇒ 条件对任何标的都成立。
 * 实测（`GET /market/screen`，24 只真实标的）：
 *
 * | 条件 | 命中 |
 * |---|---|
 * | 该预设 | **24 / 24（全部）** |
 * | 对照：「收盘价 > MA20」 | 4 / 24 |
 *
 * 也就是说这个预设**看起来在筛「放量」，实际什么都没筛**。而用户**无从察觉** ——
 * 「全中」看起来像今天普涨，不像 bug。这类缺陷单测抓不到（JSON 合法、字段都在），
 * 只能靠**断言语义**来守。
 *
 * 修法：条件树叶子的 `value` 只能是字面量，写不出「均量 × 1.5」这种倍数，
 * 因此后端补了**量比**指标 `vol_ratio`（当日量 ÷ 前 N 日均量）；
 * 「收阳」用 `compare(close > open)`（两个字段相比，引擎本就支持）。
 */

type Leaf = Record<string, any>;

/** 递归收集所有叶子（含 and/or/not 嵌套） */
function leaves(node: unknown, out: Leaf[] = []): Leaf[] {
  if (!node || typeof node !== "object") return out;
  const o = node as Record<string, unknown>;
  if (Array.isArray(o.and)) (o.and as unknown[]).forEach((c) => leaves(c, out));
  if (Array.isArray(o.or)) (o.or as unknown[]).forEach((c) => leaves(c, out));
  if (o.not) leaves(o.not, out);
  if (o.indicator || o.field || o.compare) out.push(o as Leaf);
  return out;
}

/** 「对常量 0 做大于」= 空条件签名（均线/成交量/价格恒为正，故恒真） */
function isVacuous(l: Leaf): boolean {
  for (const k of ["indicator", "field"] as const) {
    const b = l[k] as Leaf | undefined;
    if (b && b.op === "gt" && Number(b.value) === 0) return true;
  }
  return false;
}

describe("条件选股预设", () => {
  it("每个预设的 conditions 都是合法 JSON 且含叶子", () => {
    expect(PRESET_EXAMPLES.length).toBeGreaterThan(0);
    for (const p of PRESET_EXAMPLES) {
      const cond = JSON.parse(p.conditions) as unknown;
      expect(cond, `预设「${p.label}」解析失败`).toBeTruthy();
      expect(leaves(cond).length, `预设「${p.label}」没有叶子条件`).toBeGreaterThan(0);
    }
  });

  it("★ 没有预设使用「对常量 0 做大于」的空条件", () => {
    const bad = PRESET_EXAMPLES.filter((p) =>
      leaves(JSON.parse(p.conditions) as unknown).some(isVacuous),
    ).map((p) => p.label);
    expect(bad, `以下预设是空条件（恒真，筛不出任何东西）：${bad.join("、")}`).toEqual([]);
  });

  it("「放量」预设用量比表达倍数，并真的要求收阳", () => {
    const preset = PRESET_EXAMPLES.find((p) => p.label.includes("放量"));
    expect(preset, "找不到「放量」预设（标签被改了？）").toBeTruthy();
    const ls = leaves(JSON.parse(preset!.conditions) as unknown);

    // ① 必须用 vol_ratio（量比）表达「当日量 / 前 N 日均量」
    const ratio = ls.find((l) => l.indicator?.name === "vol_ratio");
    expect(ratio, "「放量」预设未使用量比指标 vol_ratio").toBeTruthy();
    // ② 阈值必须 > 1（=1 表示与均量持平，不是「放量」；0 就是空条件）
    expect(Number(ratio!.indicator.value)).toBeGreaterThan(1);
    // ③ 窗口参数必须落到 period（引擎的 win → period 映射）
    expect(Number(ratio!.indicator.params?.win ?? ratio!.indicator.params?.period))
      .toBeGreaterThan(1);

    // ④ 「收阳」必须是 close > open（不是 close > 0）
    const yang = ls.find(
      (l) => l.compare?.left?.name === "close" && l.compare?.right?.name === "open",
    );
    expect(yang, "「收阳」未表达为 close > open").toBeTruthy();
    expect(yang!.compare.op).toBe("gt");
  });

  it("「收盘价站上 20 日均线」用两个序列相比，而不是与 0 相比", () => {
    const preset = PRESET_EXAMPLES.find((p) => p.label.includes("20 日均线"));
    expect(preset).toBeTruthy();
    const ls = leaves(JSON.parse(preset!.conditions) as unknown);
    expect(
      ls.some(
        (l) =>
          l.compare?.left?.name === "close" &&
          l.compare?.right?.kind === "indicator" &&
          l.compare?.right?.name === "ma",
      ),
      "应表达为 compare(close, ma)",
    ).toBe(true);
  });
});
