import { beforeEach, describe, expect, it, vi } from "vitest";

const { health } = vi.hoisted(() => ({ health: vi.fn() }));
vi.mock("@/services/api", () => ({ systemApi: { health } }));

import { sessionBadge, useSessionStore, type TradingSession } from "@/stores/session";

/**
 * 状态栏「交易时段」角标回归测试。
 *
 * 这里锁的是**语义**，不是文案：`active=false` 有两种完全不同的含义 ——
 * 「今日休市」（节假日/周末）与「非交易时段」（盘前盘后/午休）。旧实现只看
 * `isTradingHours`（周一~周五 + 时段），国庆节周三会显示「交易时段」，
 * 用户看到的是「今天该开盘却没开盘」。修复依赖后端权威日历的 `trading_day`，
 * 所以这两档必须被测试钉死，避免以后有人把 `tradingDay` 判断删掉。
 */

/** 本地时间构造（与 isTradingHours / sessionBadge 的口径一致） */
const at = (y: number, mo: number, d: number, h: number, mi = 0) =>
  new Date(y, mo - 1, d, h, mi, 0, 0);

// 2026-09-16 是周三，2026-09-20 是周日
const WED_10 = at(2026, 9, 16, 10, 0);
const WED_12 = at(2026, 9, 16, 12, 0);
const SUN_10 = at(2026, 9, 20, 10, 0);

const srv = (p: Partial<TradingSession>): TradingSession => ({
  mode: "calendar",
  active: null,
  tradingDay: null,
  at: Date.now(),
  ...p,
});

describe("sessionBadge · 无服务端值时降级为本地估算", () => {
  it("从未取到（at=0）时用本地规则，且明确标注是本地估算", () => {
    const b = sessionBadge({ mode: "unknown", active: null, tradingDay: null, at: 0 }, WED_10);
    expect(b.label).toBe("交易时段");
    expect(b.tone).toBe("ok");
    // 关键：必须自曝「不认节假日」，否则用户会把本地近似当权威
    expect(b.title).toContain("本地估算");
    expect(b.title).toContain("不认节假日");
  });

  it("null 与 at=0 等价（都视为没拿到）", () => {
    expect(sessionBadge(null, SUN_10).label).toBe("非交易时段");
    expect(sessionBadge(null, SUN_10).tone).toBe("idle");
  });

  it("本地估算仍遵循午休/盘后（12:00 判非交易时段）", () => {
    expect(sessionBadge(null, WED_12).label).toBe("非交易时段");
  });
});

describe("sessionBadge · 服务端权威日历", () => {
  it("盘中 → 交易时段（tone=ok）", () => {
    const b = sessionBadge(srv({ active: true, tradingDay: true }), WED_10);
    expect(b.label).toBe("交易时段");
    expect(b.tone).toBe("ok");
    expect(b.title).toContain("券商真实日历");
  });

  it("★ 今日非交易日（节假日）→ 今日休市，而不是「非交易时段」", () => {
    // 这是本轮修的核心：国庆节周三 10:00，本地规则会判「交易时段」，
    // 服务端日历 trading_day=false 必须判「今日休市」。
    const b = sessionBadge(srv({ active: false, tradingDay: false }), WED_10);
    expect(b.label).toBe("今日休市");
    expect(b.tone).toBe("idle");
    expect(b.title).toContain("非交易日");
  });

  it("★ 今日是交易日但不在盘中 → 非交易时段（与「今日休市」区分开）", () => {
    const b = sessionBadge(srv({ active: false, tradingDay: true }), WED_12);
    expect(b.label).toBe("非交易时段");
    expect(b.title).toContain("今日交易日");
  });

  it("active 未知（null）但已知今日休市 → 仍判今日休市", () => {
    expect(sessionBadge(srv({ active: null, tradingDay: false }), SUN_10).label).toBe("今日休市");
  });

  it("active 未知且 tradingDay 未知 → 退回「非交易时段」（不猜成交易中）", () => {
    const b = sessionBadge(srv({ active: null, tradingDay: null }), WED_10);
    expect(b.label).toBe("非交易时段");
  });

  it("mode=weekday-fallback 时提示「不排除节假日」", () => {
    const b = sessionBadge(srv({ mode: "weekday-fallback", active: false, tradingDay: true }), WED_12);
    expect(b.title).toContain("不排除节假日");
  });
});

describe("useSessionStore.refresh · 字段映射", () => {
  const EMPTY: TradingSession = { mode: "unknown", active: null, tradingDay: null, at: 0 };

  beforeEach(() => {
    health.mockReset();
    useSessionStore.setState({ session: EMPTY });
  });

  it("把后端 snake_case trading_day 映射为 camelCase tradingDay", async () => {
    // 后端 /health 返回的是 trading_day —— 名字对不上时**既不报错也不兜底**，
    // 表现只会是「角标永远退回本地估算」，正是本项目反复踩的静默失配。
    health.mockResolvedValue({
      trading_session: { mode: "calendar", active: false, trading_day: false },
    });
    await useSessionStore.getState().refresh();
    const s = useSessionStore.getState().session;
    expect(s.mode).toBe("calendar");
    expect(s.active).toBe(false);
    expect(s.tradingDay).toBe(false);
    expect(s.at).toBeGreaterThan(0);
  });

  it("旧版后端缺 trading_day 时降级为 null（不硬编码 false）", async () => {
    health.mockResolvedValue({ trading_session: { mode: "calendar", active: true } });
    await useSessionStore.getState().refresh();
    expect(useSessionStore.getState().session.tradingDay).toBeNull();
  });

  it("请求失败时保留上次成功值（不让角标在抖动中乱跳）", async () => {
    health.mockResolvedValue({
      trading_session: { mode: "calendar", active: true, trading_day: true },
    });
    await useSessionStore.getState().refresh();
    const good = useSessionStore.getState().session;

    health.mockRejectedValue(new Error("network down"));
    await useSessionStore.getState().refresh();
    expect(useSessionStore.getState().session).toEqual(good);
  });

  it("结构异常（trading_session 缺失）按「没拿到」处理，不写入脏值", async () => {
    health.mockResolvedValue({ status: "pass" });
    await useSessionStore.getState().refresh();
    expect(useSessionStore.getState().session).toEqual(EMPTY);
  });
});
