import { beforeEach, describe, expect, it, vi } from "vitest";

const { health, session } = vi.hoisted(() => ({ health: vi.fn(), session: vi.fn() }));
// ★ 注意：session.ts 现在同时依赖 marketApi 与 SESSION_PHASE_LABEL。
//   mock 只给 systemApi 会让 SESSION_PHASE_LABEL 变成 undefined，
//   表现是「phase 标签静默变空」而不是报错 —— 故此处必须一并提供。
vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api");
  return {
    SESSION_PHASE_LABEL: actual.SESSION_PHASE_LABEL,
    fmtBarDate: actual.fmtBarDate,
    systemApi: { health },
    marketApi: { session },
  };
});

import {
  sessionBadge,
  sessionDateInfo,
  useSessionStore,
  type SessionSnapshot,
  type TradingSession,
} from "@/stores/session";
import { fmtBarDate } from "@/services/api";

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

/* ---------------- 交易日期角标（GET /market/session） ---------------- */

/**
 * 这一组锁的是用户原话：「如果是交易日优先展示交易日信息，如果非交易日
 * 展示最近一个交易日信息」。
 *
 * 背景：非交易日后端照常返回**上一交易日**的数据（这是正确的），但界面此前
 * 没有任何地方标注，用户会把周六看到的数字当成今日行情。所以「非交易日必须
 * 显式写出日期」是硬要求，不是文案偏好。
 */
const snap = (p: Partial<SessionSnapshot>): SessionSnapshot => ({
  today: "20260919",
  tradingDay: false,
  active: false,
  phase: "holiday",
  lastTradingDay: "20260918",
  asOf: "20260918",
  nextTradingDay: "20260921",
  calendarMode: "builtin",
  at: Date.now(),
  ...p,
});

describe("sessionDateInfo · 交易日/非交易日的日期展示", () => {
  it("非交易日 → 展示最近交易日，且明确写出日期与下一交易日", () => {
    const info = sessionDateInfo(snap({}), fmtBarDate);
    expect(info.offDay).toBe(true);
    expect(info.label).toContain("最近交易日");
    expect(info.label).toContain("2026-09-18");
    expect(info.asOfLabel).toBe("2026-09-18");
    expect(info.title).toContain("休市");
    expect(info.title).toContain("下一交易日 2026-09-21");
  });

  it("★ 交易日盘中 → 展示今日 + 交易中（而不是「最近交易日」）", () => {
    const info = sessionDateInfo(
      snap({ today: "20260918", tradingDay: true, active: true, phase: "open", lastTradingDay: "20260918", asOf: "20260918" }),
      fmtBarDate,
    );
    expect(info.offDay).toBe(false);
    expect(info.label).toBe("今日 · 20260918 交易中");
    expect(info.asOfLabel).toBe("2026-09-18");
  });

  it("交易日已收盘 → 展示今日 + 已收盘（仍属交易日，不回退上一交易日）", () => {
    const info = sessionDateInfo(
      snap({ today: "20260918", tradingDay: true, active: false, phase: "closed", lastTradingDay: "20260918", asOf: "20260918" }),
      fmtBarDate,
    );
    expect(info.offDay).toBe(false);
    expect(info.label).toBe("今日 · 20260918 已收盘");
  });

  it("午间休市与盘前各自有独立标签（active=false 不能一律说成「非交易时段」）", () => {
    const mk = (phase: string) =>
      sessionDateInfo(
        snap({ today: "20260918", tradingDay: true, active: false, phase, lastTradingDay: "20260918", asOf: "20260918" }),
        fmtBarDate,
      ).label;
    expect(mk("lunch_break")).toContain("午间休市");
    expect(mk("pre_open")).toContain("盘前");
  });

  it("★ 未知 phase 时退回 active 判定，不得渲染出空标签", () => {
    const info = sessionDateInfo(
      snap({ today: "20260918", tradingDay: true, active: true, phase: "brand_new_phase", lastTradingDay: "20260918", asOf: "20260918" }),
      fmtBarDate,
    );
    expect(info.label).toBe("今日 · 20260918 交易中");
  });

  it("取不到快照 → 返回空标签（宁可什么都不显示，也不显示可能错的日期）", () => {
    const info = sessionDateInfo(null, fmtBarDate);
    expect(info.label).toBe("");
    expect(info.asOfLabel).toBe("");
  });

  it("calendarMode 决定可信度文案：exchange 说券商日历，builtin 说内置表", () => {
    expect(sessionDateInfo(snap({ calendarMode: "exchange" }), fmtBarDate).title).toContain("券商真实交易日历");
    expect(sessionDateInfo(snap({ calendarMode: "builtin" }), fmtBarDate).title).toContain("内置节假日表");
  });
});

describe("fmtBarDate · YYYYMMDD → YYYY-MM-DD", () => {
  it("8 位数字转横线格式", () => {
    expect(fmtBarDate("20260918")).toBe("2026-09-18");
  });
  it("非 8 位（已带横线/空值）原样返回，不二次加工", () => {
    expect(fmtBarDate("2026-09-18")).toBe("2026-09-18");
    expect(fmtBarDate("")).toBe("");
    expect(fmtBarDate(null)).toBe("");
  });
});

describe("useSessionStore.refreshSnapshot · 字段映射与去重", () => {
  const emptySnap = { session: { mode: "unknown", active: null, tradingDay: null, at: 0 } };

  beforeEach(() => {
    session.mockReset();
    useSessionStore.setState({ snapshot: null, ...emptySnap });
  });

  it("把后端 snake_case 字段映射为 camelCase（名字对不上只会静默失效）", async () => {
    session.mockResolvedValue({
      today: "20260919",
      trading_day: false,
      active: false,
      phase: "holiday",
      last_trading_day: "20260918",
      as_of: "20260918",
      next_trading_day: "20260921",
      calendar: { mode: "exchange", exact: true },
      now: "2026-09-19 09:00:00",
    });
    await useSessionStore.getState().refreshSnapshot();
    const s = useSessionStore.getState().snapshot;
    expect(s?.today).toBe("20260919");
    expect(s?.tradingDay).toBe(false);
    expect(s?.lastTradingDay).toBe("20260918");
    expect(s?.asOf).toBe("20260918");
    expect(s?.nextTradingDay).toBe("20260921");
    expect(s?.calendarMode).toBe("exchange");
  });

  it("10 秒内重复调用只打一次接口（一页多处挂角标）", async () => {
    session.mockResolvedValue({ today: "20260919", trading_day: false, calendar: { mode: "builtin" } });
    await useSessionStore.getState().refreshSnapshot();
    await useSessionStore.getState().refreshSnapshot();
    expect(session).toHaveBeenCalledTimes(1);
  });

  it("并发调用共用同一个 in-flight 请求（只看时间戳挡不住同 tick 并发）", async () => {
    session.mockResolvedValue({ today: "20260919", trading_day: false, calendar: { mode: "builtin" } });
    await Promise.all([
      useSessionStore.getState().refreshSnapshot(),
      useSessionStore.getState().refreshSnapshot(),
      useSessionStore.getState().refreshSnapshot(),
    ]);
    expect(session).toHaveBeenCalledTimes(1);
  });

  it("请求失败保留上次值（日期不能因抖动变空）", async () => {
    session.mockResolvedValue({ today: "20260919", trading_day: false, last_trading_day: "20260918", calendar: { mode: "builtin" } });
    await useSessionStore.getState().refreshSnapshot();
    const good = useSessionStore.getState().snapshot;

    useSessionStore.setState({ snapshot: { ...(good as SessionSnapshot), at: 0 } });
    session.mockRejectedValue(new Error("network down"));
    await useSessionStore.getState().refreshSnapshot();
    expect(useSessionStore.getState().snapshot?.lastTradingDay).toBe("20260918");
  });

  it("结构异常（缺 today）不写入脏值", async () => {
    session.mockResolvedValue({ trading_day: false });
    await useSessionStore.getState().refreshSnapshot();
    expect(useSessionStore.getState().snapshot).toBeNull();
  });
});
