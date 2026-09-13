import { http } from "../http";
import type { BrokerConnection, BrokerProfile } from "@/shared/types";

export interface BrokerTestResult {
  ok: boolean;
  runtime_mode?: "inproc" | "bridge";
  reason?: string;
  runtime_source?: string;
  suggestions?: string[];
}

export interface VersionProfile {
  client_type: "mini" | "full" | "both" | "unknown";
  version_str?: string;
  sdk_version?: string;
  trade_dir?: string;
  broker_name?: string;
  account_id?: string;
  account_type?: string;
  capabilities?: string[];
}

/** 券商连接管理 API。路径与 backend/app/routes/broker.py 对应。 */
export const brokerApi = {
  profiles: () => http.get<BrokerProfile[]>("/brokers/profiles"),

  list: () => http.get<BrokerConnection[]>("/brokers"),

  add: (body: {
    broker_id: string;
    client_path: string;
    account_id: string;
    account_type?: string;
    session_id?: number;
    label?: string;
  }) => http.post<BrokerConnection>("/brokers", body),

  remove: (connId: string) => http.del<{ ok: boolean }>(`/brokers/${connId}`),

  /** ★ 批量删除的 body 键是 ids（不是 conn_ids） */
  batchRemove: (ids: string[]) =>
    http.post<{ removed: string[]; deleted: number }>("/brokers/batch-delete", { ids }),

  /**
   * 单连接健康检查（gateway/health.py:status）。
   * 未初始化健康监控时返回 503，前端需如实提示而非当成功。
   */
  health: (connId: string) =>
    http.get<Record<string, unknown>>(`/brokers/${connId}/health`),

  connect: (connId: string) => http.post<{ ok: boolean; reason?: string }>(`/brokers/${connId}/connect`),

  disconnect: (connId: string) =>
    http.post<{ ok: boolean }>(`/brokers/${connId}/disconnect`),

  setActive: (connId: string) => http.post<{ ok: boolean }>(`/brokers/${connId}/active`),

  test: (body: { broker_id: string; client_path: string }) =>
    http.post<BrokerTestResult>("/brokers/test", body),

  autoDetect: () => http.get<Record<string, unknown>>("/brokers/auto-detect"),

  runtimes: () => http.get<Record<string, unknown>>("/brokers/runtimes"),

  versionInfo: (body: { client_path: string }) =>
    http.post<VersionProfile>("/brokers/version-info", body),

  diagnostics: () => http.get<Record<string, unknown>>("/brokers/diagnostics"),

  launch: (connId: string) => http.post<{ ok: boolean }>("/brokers/launch", { conn_id: connId }),
};
