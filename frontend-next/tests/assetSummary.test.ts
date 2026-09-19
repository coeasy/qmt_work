import { describe, expect, it } from "vitest";
import {
  positionPrice,
  positionProfit,
  positionProfitPct,
} from "@/domains/account/AssetSummary";
import type { AccountGridPosition } from "@/shared/types";

/**
 * 资产汇总「三列同源」不变量。
 *
 * ★ 为什么必须测：最新价 / 合计市值 / 浮动盈亏 三列若各自取价（一个用实时行情、
 * 一个用后端快照、一个用后端聚合 profit），同一行会出现「价格涨了但盈亏没动」
 * 这种自相矛盾的画面。这里锁定：**有成本时盈亏与盈亏比都由当前价现算**，
 * 保证与市值列出自同一个价格。
 */

const pos = (over: Partial<AccountGridPosition> = {}): AccountGridPosition => ({
  code: "600519.SH",
  name: "贵州茅台",
  total_volume: 100,
  total_market_value: 150000,
  total_cost: null,
  price: null,
  profit: null,
  profit_pct: null,
  accounts: [],
  ...over,
});

describe("positionPrice", () => {
  it("实时行情优先于后端快照", () => {
    expect(positionPrice(pos({ price: 1500 }), 1600)).toBe(1600);
  });

  it("无实时行情时用后端快照价", () => {
    expect(positionPrice(pos({ price: 1500 }), undefined)).toBe(1500);
  });

  it("两者都无时用 市值/股数 反推（仅自洽时）", () => {
    expect(positionPrice(pos({ total_market_value: 150000, total_volume: 100 }), undefined)).toBe(1500);
  });

  it("股数为 0 时不反推（避免除零伪造出价格）", () => {
    expect(positionPrice(pos({ total_volume: 0, total_market_value: 0 }), undefined)).toBeUndefined();
  });

  it("实时价为 0（停牌）视为缺失，回退快照", () => {
    expect(positionPrice(pos({ price: 1500 }), 0)).toBe(1500);
  });
});

describe("positionProfit", () => {
  it("有成本时按当前价现算（与市值列同源）", () => {
    // 成本 1400×100 = 140000，现价 1500 ⇒ 盈亏 10000
    const r = pos({ total_cost: 140000, total_volume: 100 });
    expect(positionProfit(r, 1500)).toBe(10000);
  });

  it("成本已知时优先现算，不采用可能过期的后端 profit", () => {
    const r = pos({ total_cost: 140000, total_volume: 100, profit: 999 });
    expect(positionProfit(r, 1500)).toBe(10000);
  });

  it("无成本时退回后端聚合值", () => {
    expect(positionProfit(pos({ profit: -320.5 }), 1500)).toBe(-320.5);
  });

  it("既无成本也无后端值时返回 undefined（界面显示「—」而不是 0）", () => {
    expect(positionProfit(pos(), 1500)).toBeUndefined();
  });

  it("价格为 0/缺失时不硬算", () => {
    const r = pos({ total_cost: 140000, profit: 7 });
    expect(positionProfit(r, 0)).toBe(7);
    expect(positionProfit(r, undefined)).toBe(7);
  });
});

describe("positionProfitPct", () => {
  it("有成本时按当前价现算百分比", () => {
    const r = pos({ total_cost: 140000, total_volume: 100 });
    expect(positionProfitPct(r, 1500)).toBeCloseTo(7.142857, 4);
  });

  it("无成本时用后端 profit_pct", () => {
    expect(positionProfitPct(pos({ profit_pct: -3.25 }), 1500)).toBe(-3.25);
  });

  it("都无时返回 undefined", () => {
    expect(positionProfitPct(pos(), 1500)).toBeUndefined();
  });

  it("成本为 0 时不除以零（视为无成本）", () => {
    expect(positionProfitPct(pos({ total_cost: 0, profit_pct: 1.5 }), 1500)).toBe(1.5);
  });
});
