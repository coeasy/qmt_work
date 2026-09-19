import { create } from "zustand";
import { marketApi, SESSION_PHASE_LABEL } from "@/services/api";
import { systemApi } from "@/services/api";
import { isTradingHours } from "@/shared/format";

/**
 * 服务端权威交易时段（`GET /health` 的 `trading_session`）。
 *
 * 为什么不用前端本地规则：`shared/format.ts::isTradingHours` 是「周一~周五 + 时间段」
 * 的**本地近似**，不认法定节假日 —— 国庆节周三 10:00 它会显示「交易时段」，
 * 而实际休市。后端 `gateway/trading_session.py` 用的是**券商真实交易日历**
 * （`mode: calendar`，实测 8728 个交易日），`/health` 早就把它暴露出来了，
 * 只是状态栏一直没接线（「能力存在但没人调用」的老毛病）。
 *
 * 降级策略：拿不到服务端值时回退本地估算，并在 tooltip 里**明确标注是本地估算**
 * —— 宁可标注不确定，也不要让用户以为节假日开盘了。
 */
export interface TradingSession {
  /** calendar = 券商真实日历；weekday-fallback = 周末规则；unknown = 后端未就绪 */
  mode: string;
  /** 是否应保持高频轮询（交易日 && 盘中活跃） */
  active: boolean | null;
  /** 今日是否交易日（节假日为 false）；mode=weekday-fallback 时不可靠 */
  tradingDay: boolean | null;
  /** 上次成功取到的时刻（ms）；0 = 从未取到 */
  at: number;
}

const EMPTY: TradingSession = { mode: "unknown", active: null, tradingDay: null, at: 0 };

/**
 * 交易会话快照（`GET /market/session` 的前端形态，日期统一 YYYYMMDD）。
 *
 * 与 `TradingSession` 的区别：这里**必须有日期**。`TradingSession` 只回答
 * 「现在是不是交易时段」，本结构回答「你看到的是哪一天的数据」。
 */
export interface SessionSnapshot {
  /** 今天 YYYYMMDD */
  today: string;
  tradingDay: boolean;
  active: boolean;
  /** holiday / pre_open / open / lunch_break / closed */
  phase: string;
  /** 数据参照日（非交易日 = 上一交易日） */
  lastTradingDay: string;
  /** 同 lastTradingDay */
  asOf: string;
  nextTradingDay: string;
  /** exchange = 券商真实日历；builtin = 内置节假日表 */
  calendarMode: string;
  /** 上次成功取到的时刻（ms）；0 = 从未取到 */
  at: number;
}


export type SessionTone = "ok" | "idle" | "warn";
export interface SessionBadge {
  label: string;
  tone: SessionTone;
  title: string;
}

/**
 * 状态栏「交易时段」角标的纯函数（便于单测直接锁定语义）。
 *
 * 关键区分：`active=false` 有两种截然不同的含义 ——
 * **今日休市**（节假日/周末）与**非交易时段**（盘前盘后/午休）。
 * 只看 `active` 会把两者混在一起，用户会误以为「今天该开盘却没开」。
 */
export function sessionBadge(session: TradingSession | null, now: Date): SessionBadge {
  const s = session && session.at > 0 ? session : null;
  if (!s) {
    const trading = isTradingHours(now);
    return {
      label: trading ? "交易时段" : "非交易时段",
      tone: trading ? "ok" : "idle",
      title: "本地估算（周一~周五 + 时段，不认节假日）—— 未取到服务端交易日历",
    };
  }
  const via =
    s.mode === "calendar"
      ? "服务端交易日历（券商真实日历）"
      : s.mode === "weekday-fallback"
        ? "服务端周末规则（券商日历不可用，不排除节假日）"
        : "服务端（日历模式未知）";
  if (s.active === true) {
    return { label: "交易时段", tone: "ok", title: `${via}：今日交易日，当前盘中` };
  }
  if (s.tradingDay === false) {
    return { label: "今日休市", tone: "idle", title: `${via}：今日非交易日（周末/节假日）` };
  }
  return { label: "非交易时段", tone: "idle", title: `${via}：今日交易日，当前非盘中` };
}

/** 进行中的 /market/session 请求（同一 tick 内多处挂角标时共用，避免重复打接口） */
let _inflight: Promise<void> | null = null;

interface SessionState {
  session: TradingSession;
  /** 拉一次服务端交易时段；失败保留上次值（网络抖动不该让角标乱跳） */
  refresh: () => Promise<void>;

  /**
   * 交易会话快照（`GET /market/session`）：比 `session` 多给**日期**。
   *
   * 为什么要有第二份：`session`（来自 /health）只有 mode/active/trading_day，
   * 能回答「现在是不是交易时段」，**回答不了「你现在看到的是哪一天的行情」**
   * —— 非交易日用户最需要的恰恰是后者。两份并存不是重复：`session` 轻量、
   * 由状态栏高频轮询；`snapshot` 带日期、由行情页按需拉取。
   */
  snapshot: SessionSnapshot | null;
  refreshSnapshot: () => Promise<void>;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  session: EMPTY,

  async refresh() {
    try {
      const res = await systemApi.health();
      const ts = res?.trading_session;
      if (!ts || typeof ts !== "object") return; // 结构异常按「没拿到」处理
      set({
        session: {
          mode: typeof ts.mode === "string" ? ts.mode : "unknown",
          active: typeof ts.active === "boolean" ? ts.active : null,
          tradingDay: typeof ts.trading_day === "boolean" ? ts.trading_day : null,
          at: Date.now(),
        },
      });
    } catch {
      // 保留上次成功值：一次失败就让角标退回「本地估算」反而是体验倒退
    }
  },

  snapshot: null,

  async refreshSnapshot() {
    // 去重（两重）：一页里多处挂 <TradingDateBadge />（如工作台同时含报价牌）
    // 会各拉一次。① 10 秒内的重复调用直接复用；② 同一 tick 内并发调用共用
    // 同一个 in-flight Promise（只看 `at` 挡不住「都还没返回」的并发）。
    const cur = get().snapshot;
    if (cur && Date.now() - cur.at < 10_000) return;
    if (_inflight) return _inflight;
    _inflight = (async () => {
      try {
        const r = await marketApi.session();
        if (!r || typeof r !== "object" || !r.today) return;
        set({
          snapshot: {
            today: r.today,
            tradingDay: !!r.trading_day,
            active: !!r.active,
            phase: typeof r.phase === "string" ? r.phase : "",
            lastTradingDay: typeof r.last_trading_day === "string" ? r.last_trading_day : "",
            asOf: typeof r.as_of === "string" ? r.as_of : "",
            nextTradingDay: typeof r.next_trading_day === "string" ? r.next_trading_day : "",
            calendarMode: r.calendar?.mode ?? "unknown",
            at: Date.now(),
          },
        });
      } catch {
        // 保留上次值；界面用 `snapshot === null` 判断「未取到」并退化为不显示日期
      } finally {
        _inflight = null;
      }
    })();
    return _inflight;
  },
}));

/**
 * 会话快照的纯函数派生（便于单测锁定文案）。
 *
 * 核心规则：**交易日优先展示今天，非交易日展示最近交易日**。
 * 非交易日必须显式写出日期，否则用户会把上一交易日的数字当成今日行情
 * —— 这正是本次要修的体验问题。
 */
export interface SessionDateInfo {
  /** 主标签，如「今日 · 20260919 休市」/「最近交易日 · 20260918 已收盘」 */
  label: string;
  /** 数据参照日（YYYY-MM-DD），无则空串 */
  asOfLabel: string;
  /** 悬停说明 */
  title: string;
  /** 是否非交易日（界面据此决定是否加提示色） */
  offDay: boolean;
}

export function sessionDateInfo(
  snap: SessionSnapshot | null,
  fmt: (v: string) => string,
): SessionDateInfo {
  if (!snap) {
    return { label: "", asOfLabel: "", title: "未取到服务端交易日历", offDay: false };
  }
  const phase = SESSION_PHASE_LABEL[snap.phase] ?? (snap.active ? "交易中" : "非交易时段");
  const via =
    snap.calendarMode === "exchange"
      ? "券商真实交易日历"
      : snap.calendarMode === "builtin"
        ? "内置节假日表"
        : "日历来源未知";
  if (snap.tradingDay) {
    return {
      label: `今日 · ${snap.today} ${phase}`,
      asOfLabel: fmt(snap.asOf || snap.today),
      title: `${via}：今日为交易日（${phase}），数据截至 ${fmt(snap.asOf || snap.today)}`,
      offDay: false,
    };
  }
  const last = fmt(snap.lastTradingDay);
  const next = fmt(snap.nextTradingDay);
  return {
    label: `最近交易日 · ${last} 已收盘`,
    asOfLabel: last,
    title:
      `${via}：今日（${fmt(snap.today)}）休市，下方展示的是最近交易日 ${last} 的数据` +
      (next ? `；下一交易日 ${next}` : ""),
    offDay: true,
  };
}

