import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * 「未连接券商 ⇒ 下不了单」是**错误归因**（2026-09-20 修复）。
 *
 * ## 缺陷
 *
 * `OrderForm` 的两条「未连接券商」提示此前硬写：
 *
 *   未连接券商，下单将返回 503 —— 请先到「连接管理」连接。
 *   当前无已连接券商，下单将返回 503 引导。请先到「连接管理」连接。
 *
 * 但那只对 **live（实盘）** 成立。后端 `routes/trade.py` 已按执行模式分流：
 *
 *   - live    → 无券商 ⇒ 503 + 券商引导（原行为，归因不变）
 *   - paper   → 成交由 `PaperEngine` **本地撮合**，不依赖券商，**会真的成交**
 *   - dry_run → 只返回计划，既不成交也不报错
 *
 * 于是这两条提示会把用户**骗去连券商**，而其实把信号模式切到模拟盘当场就能成交。
 * 与 `/trade/precheck`（已按 `require_account=_live` 分流）也自相矛盾：
 * 预检说「可以下单」，下单提示却说「会 503」。
 *
 * ## 为什么用源扫描而不是渲染测试
 *
 * 该文案出现在 OrderForm 的两个分支（紧凑 / 标准）里，渲染它需要桩掉 broker store、
 * quotes store、useAsync、quote subscription…… 而这里要锁的是一条**纪律**：
 * 「不得对下单后果做无条件断言」。源扫描正是锁纪律的手段（与 `freshness.test.ts`
 * 锁价格判据同源）。
 */

const SRC = resolve(__dirname, "../src");
const ORDER_FORM = join(SRC, "domains/trading/OrderForm.tsx");

describe("OrderForm · 「未连接券商」提示必须按信号模式分流", () => {
  const src = readFileSync(ORDER_FORM, "utf8");

  it("「下单将返回 503」只能出现在 live 分支里（不得无条件出现）", () => {
    const lines = src.split("\n");
    const isComment = (l: string) => /^\s*(\/\/|\*|\/\*)/.test(l);
    const hits = lines
      .map((l, i) => ({ l, i }))
      .filter(({ l }) => !isComment(l) && /下单将返回 503/.test(l));
    // 只允许 live 分支那一处（此前紧凑/标准两个分支各硬写了一遍，共 2 处且都不分模式）
    expect(hits.length).toBe(1);
    // 且它必须落在 `case "live"` 与 `default:` 之间
    const liveAt = lines.findIndex((l) => /case\s+"live"/.test(l));
    const defAt = lines.findIndex((l) => /^\s*default:/.test(l));
    const hitAt = hits[0]?.i ?? -1;
    expect(liveAt).toBeGreaterThanOrEqual(0);
    expect(hitAt).toBeGreaterThan(liveAt);
    expect(hitAt).toBeLessThan(defAt);
  });

  it("三种模式各有对应文案（paper 明确说「不依赖券商」）", () => {
    expect(src).toMatch(/case\s+"paper"/);
    expect(src).toMatch(/case\s+"dry_run"/);
    expect(src).toMatch(/case\s+"live"/);
    // paper 必须讲清「本地模拟撮合 / 不依赖券商」，否则用户仍会以为要连券商
    expect(src).toMatch(/模拟盘[\s\S]{0,40}不依赖券商/);
  });

  it("模式未知时不臆断后果（不写死 503）", () => {
    // default 分支必须存在，且不得复述 503 结论
    expect(src).toMatch(/default:/);
    expect(src).toMatch(/下单后果取决于信号模式/);
  });

  it("提示条渲染条件是「有结论」而不是「未连接券商」（本轮修复的核心）", () => {
    const uses = src.match(/\{execNote\}/g) || [];
    // 紧凑 / 标准两个分支各一处
    expect(uses.length).toBeGreaterThanOrEqual(2);
    // ★ 不得再用 `!connected &&` 当渲染条件 —— 那正是「已连券商 + 默认 paper
    //   模式下一个字都不提示」的成因（下单不会报给券商，界面却不说）。
    //   注意：注释里出现 `!connected` 不算（本文件只扫代码行）。
    const codeLines = src
      .split("\n")
      .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l))
      .join("\n");
    expect(codeLines).not.toMatch(/!connected/);
    // 且不得残留旧变量名（改名就是为了让「按模式提示」成为唯一入口）
    expect(src).not.toMatch(/noBrokerNote/);
  });
});

describe("全站 · 不得把「未连接券商」写成下单必然失败", () => {
  it("仅查询类页面可以断言 503，下单/提交类不得", () => {
    const positions = readFileSync(join(SRC, "domains/account/Positions.tsx"), "utf8");
    // Positions 是**查询**类页面（持仓/委托/成交），无券商时确实恒 503 —— 保留正确
    expect(positions).toMatch(/查询类端点会返回 503/);
  });
});
