import { create } from "zustand";
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

interface SessionState {
  session: TradingSession;
  /** 拉一次服务端交易时段；失败保留上次值（网络抖动不该让角标乱跳） */
  refresh: () => Promise<void>;
}

export const useSessionStore = create<SessionState>((set) => ({
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
}));
