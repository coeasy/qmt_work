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

/** 从 userdata[(_mini)]/users/<登录>/Config.xml 读出的资金账号。 */
export interface AutoDetectAccount {
  account_id: string;
  broker_name?: string;
  broker_type?: string;
  account_type?: string;
  account_name?: string;
  login_account?: string;
  config_path?: string;
}

/**
 * 本机探测到的 QMT 客户端候选
 * （backend/xtquant_client/discovery.py::discover + routes/broker.py 的补全）。
 *
 * 字段全部按后端实现逐一对齐，不做可选化猜测：`client_path` 已经是
 * 「建议用于连接的那一个」（generic 且有 mini 时后端会改写成 userdata_mini）。
 */
export interface AutoDetectCandidate {
  root: string;
  name: string;
  /** 探测出的券商档案 id；可能是 generic */
  broker_id: string;
  /** 后端从 Config.xml 读出的真实券商名（可能为空） */
  broker_name?: string;
  running: boolean;
  pid: string;
  process?: string;
  /** ★ 建议连接路径（优先极速版 userdata_mini） */
  client_path: string;
  client_mode: "mini" | "full" | "auto" | string;
  client_path_mini: string;
  client_path_full: string;
  has_userdata_mini: boolean;
  has_userdata: boolean;
  version_str: string;
  sdk_version: string;
  xtquant_found: boolean;
  xtquant_importable: boolean;
  runtime_mode?: string;
  /** 后端补全：该客户端下发现的全部资金账号 */
  accounts?: AutoDetectAccount[];
  /** 后端补全：建议默认填入的账号（accounts[0]） */
  default_account_id?: string;
}

export interface AutoDetectResult {
  candidates: AutoDetectCandidate[];
  count: number;
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
    /** 客户端模式：mini（极速版，程序化通道更稳）/ full / auto */
    client_mode?: string;
    session_id?: number;
    label?: string;
    /** 后端默认 true：建连即拉起子进程握手（routes/broker.py:267） */
    autoconnect?: boolean;
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

  /**
   * 自动发现本机 QMT / MiniQMT 客户端（运行中进程 + 安装目录扫描）。
   * 只读、无副作用；候选里已含自动读出的资金账号，可免手填。
   */
  autoDetect: () => http.get<AutoDetectResult>("/brokers/auto-detect"),

  runtimes: () => http.get<Record<string, unknown>>("/brokers/runtimes"),

  versionInfo: (body: { client_path: string }) =>
    http.post<VersionProfile>("/brokers/version-info", body),

  diagnostics: () => http.get<Record<string, unknown>>("/brokers/diagnostics"),

  launch: (connId: string) => http.post<{ ok: boolean }>("/brokers/launch", { conn_id: connId }),
};
