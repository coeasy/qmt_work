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
   */
  connectCandidate: (
    candidate: AutoDetectCandidate,
    accountId?: string,
  ) => Promise<{ ok: boolean; reason?: string }>;
  connect: (connId: string) => Promise<{ ok: boolean; reason?: string }>;
  disconnect: (connId: string) => Promise<void>;
  setActive: (connId: string) => Promise<void>;
  remove: (connId: string) => Promise<void>;
  /** 批量删除（POST /brokers/batch-delete）。删除后回读连接列表 */
  batchRemove: (ids: string[]) => Promise<void>;
  /** 单连接健康检查（GET /brokers/{conn_id}/health），返回原始状态对象 */
  health: (connId: string) => Promise<Record<string, unknown>>;
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
      if (connId) {
        try {
          await brokerApi.setActive(connId);
        } catch {
          // 设为活跃失败不阻断：连接已建立，用户可在列表里手动切换
        }
      }
      await get().load();
      return { ok: true };
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
