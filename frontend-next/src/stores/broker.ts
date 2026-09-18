import { create } from "zustand";
import { brokerApi } from "@/services/api";
import type { AutoDetectCandidate } from "@/services/api";
import type { BrokerConnection, BrokerProfile } from "@/shared/types";

/**
 * 券商连接 store。
 *
 * 注意后端约束（审计发现）：活跃连接全局唯一（manager.py:340-348），
 * 因此 UI 必须显式呈现「当前活跃连接」，避免用户误以为可同时活跃多账户。
 */

interface BrokerState {
  profiles: BrokerProfile[];
  connections: BrokerConnection[];
  activeId: string;
  loading: boolean;
  error: string;

  /** 自动探测到的本机 QMT 客户端候选（只读） */
  candidates: AutoDetectCandidate[];
  detecting: boolean;
  /** 是否已完成过一次探测：用于区分「尚未探测」与「探测到 0 个」 */
  detected: boolean;
  detectError: string;

  loadProfiles: () => Promise<void>;
  load: () => Promise<void>;
  /**
   * 自动探测本机 QMT / MiniQMT 客户端（GET /brokers/auto-detect）。
   * 只读、无副作用；失败写进 detectError 而不抛出，避免整页被打断。
   */
  detect: () => Promise<void>;
  /**
   * 用探测结果一键建连：add(autoconnect) → 设为活跃 → 回读列表。
   *
   * 连接一旦建立就会被后端持久化，之后每次启动都会自动连接
   * （bootstrap/phase_broker.py 会 load_persisted() 并启动所有 active 连接），
   * 所以「自动连接」只需用户确认这一次。
   *
   * ★ 幂等：后端按「券商 + 客户端路径 + 资金账号」复用既有连接（返回 `reused`）。
   *   连点同一个候选不会再堆出重复条目，调用方应据 `reused` 如实提示。
   */
  connectCandidate: (
    candidate: AutoDetectCandidate,
    accountId?: string,
  ) => Promise<{ ok: boolean; reason?: string; reused?: boolean }>;
  connect: (connId: string) => Promise<{ ok: boolean; reason?: string }>;
  disconnect: (connId: string) => Promise<void>;
  setActive: (connId: string) => Promise<void>;
  remove: (connId: string) => Promise<void>;
  /** 批量删除（POST /brokers/batch-delete）。删除后回读连接列表 */
  batchRemove: (ids: string[]) => Promise<void>;
  /** 单连接健康检查（GET /brokers/{conn_id}/health），返回原始状态对象 */
  health: (connId: string) => Promise<Record<string, unknown>>;
}

export type BrokerTone = "ok" | "warn" | "err" | "idle";
/** 角标点击后要触发的动作。`null` = 不可点击 —— 没有出路时不要假装能点。 */
export type BrokerAction = "retry" | "manage" | null;

export interface BrokerBadge {
  label: string;
  tone: BrokerTone;
  title: string;
  action: BrokerAction;
}

/**
 * 状态栏「券商」角标的纯函数（便于单测锁定语义）。
 *
 * ★ 基础状态设计原则（2026-09-17）：**每一个非正常态都要给出路**。
 * 旧实现里未连券商时显示的是 `券商 0/0` 且不可点击 —— 用户只知道「没连上」，
 * 不知道「为什么」也「该做什么」。现在三档非正常态各自带明确处置：
 *   读取中 → 不可点（本来就没得点）
 *   读取失败 → 点击重试
 *   未配置 / 已配置但未连上 → 点击进「连接管理」（含自动识别本机 QMT 客户端）
 * 注意「未配置」与「配了但没连上」必须分开 —— 前者要引导去建连，后者要引导去排障，
 * 合并成一句「0/0」会让后者以为自己去错地方了。
 */
export function brokerBadge(
  connections: Array<Pick<BrokerConnection, "connected">>,
  loading: boolean,
  error: string,
): BrokerBadge {
  const total = connections.length;
  const connected = connections.filter((c) => c.connected).length;
  // 顺序有讲究：失败信息比进度信息更该被看到。`load()` 在开始时会把 error 清空，
  // 所以两者正常不会同时存在；但万一并存，不能用一个「读取中…」把失败盖住
  // （用户会一直等一个永远不会来的结果）。
  if (total === 0 && error) {
    return {
      label: "券商 状态未知",
      tone: "err",
      title: `读取券商连接失败：${error}\n点击重试`,
      action: "retry",
    };
  }
  if (total === 0 && loading) {
    return {
      label: "券商 读取中…",
      tone: "idle",
      title: "正在读取券商连接列表",
      action: null,
    };
  }
  if (total === 0) {
    return {
      label: "未连接券商",
      tone: "err",
      title: "尚未配置任何券商连接 —— 点击进入「连接管理」自动识别本机 QMT 客户端",
      action: "manage",
    };
  }
  if (connected === 0) {
    return {
      label: `券商 0/${total}`,
      tone: "err",
      title: "已配置连接但均未连上 —— 点击进入「连接管理」查看原因并重连",
      action: "manage",
    };
  }
  return {
    label: `券商 ${connected}/${total}`,
    tone: "ok",
    title: "券商连接正常 —— 点击进入「连接管理」",
    action: "manage",
  };
}

export const useBrokerStore = create<BrokerState>((set, get) => ({
  profiles: [],
  connections: [],
  activeId: "",
  loading: false,
  error: "",

  candidates: [],
  detecting: false,
  detected: false,
  detectError: "",

  async loadProfiles() {
    try {
      const profiles = await brokerApi.profiles();
      set({ profiles });
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
    }
  },

  async load() {
    set({ loading: true, error: "" });
    try {
      const connections = await brokerApi.list();
      const active = connections.find((c) => c.active)?.conn_id ?? "";
      set({ connections, activeId: active, loading: false });
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : String(e) });
    }
  },

  async detect() {
    set({ detecting: true, detectError: "" });
    try {
      const res = await brokerApi.autoDetect();
      // 防御：接口返回结构异常时按「未发现」处理，而不是让 UI 崩在 .map 上
      const candidates = Array.isArray(res?.candidates) ? res.candidates : [];
      set({ candidates, detecting: false, detected: true });
    } catch (e) {
      set({
        detecting: false,
        detected: true,
        candidates: [],
        detectError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  async connectCandidate(candidate, accountId) {
    const acc =
      accountId ||
      candidate.default_account_id ||
      candidate.accounts?.[0]?.account_id ||
      "";
    if (!acc) {
      return { ok: false, reason: "该客户端下未发现资金账号，请在下方表单手动填写" };
    }
    try {
      const created = await brokerApi.add({
        broker_id: candidate.broker_id || "generic",
        client_path: candidate.client_path,
        account_id: acc,
        account_type: candidate.accounts?.[0]?.account_type || "STOCK",
        client_mode: candidate.client_mode || "auto",
        label: candidate.broker_name || candidate.name || undefined,
        autoconnect: true,
      });
      // 活跃连接全局唯一：一键连接即把下单通道切到它，避免「连上了但没生效」
      const connId = created?.conn_id;
      // reused=true 表示后端复用了既有连接（同客户端 + 同资金账号），没新建。
      const reused = created?.reused === true;
      if (connId) {
        try {
          await brokerApi.setActive(connId);
        } catch {
          // 设为活跃失败不阻断：连接已建立，用户可在列表里手动切换
        }
      }
      await get().load();
      return { ok: true, reused };
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      set({ error: reason });
      return { ok: false, reason };
    }
  },

  async connect(connId) {
    try {
      const res = await brokerApi.connect(connId);
      await get().load();
      return res;
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      set({ error: reason });
      return { ok: false, reason };
    }
  },

  async disconnect(connId) {
    await brokerApi.disconnect(connId);
    await get().load();
  },

  async setActive(connId) {
    await brokerApi.setActive(connId);
    await get().load();
  },

  async remove(connId) {
    await brokerApi.remove(connId);
    await get().load();
  },

  async batchRemove(ids) {
    if (!ids.length) return;
    await brokerApi.batchRemove(ids);
    await get().load();
  },

  async health(connId) {
    return brokerApi.health(connId);
  },
}));
