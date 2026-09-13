import { create } from "zustand";
import { brokerApi } from "@/services/api";
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

  loadProfiles: () => Promise<void>;
  load: () => Promise<void>;
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
