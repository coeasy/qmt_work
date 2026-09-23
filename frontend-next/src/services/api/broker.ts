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

/** 单条连接的诊断条目（`/brokers/diagnostics` 的 connections 元素） */
export interface BrokerDiagConnection {
  conn_id: string;
  name?: string;
  broker_id?: string;
  broker_name?: string;
  /** 实际生效的适配器（xtp / ...） */
  adapter?: string;
  client_path?: string;
  connected?: boolean;
  active?: boolean;
  health_status?: string;
  last_error?: string;
  reconnect_attempts?: number;
  /** 行情泵是否在跑 —— 「握手成功」与「行情在推」是两件事 */
  pump_running?: boolean;
  /** 仅 deep=1：该 client_path 的 ABI 桥接方案 */
  runtime_plan?: Record<string, unknown> | null;
}

/**
 * `/brokers/diagnostics` 快照。
 *
 * 存在意义：桥接子进程握手失败时，后端错误信息常被多层截断（stderr 缓冲 + 行长限制），
 * 用户只能看到半句话。这里把「宿主 ABI / 随包桥接运行时 / 每条连接的适配器与泵状态」
 * 一次性摊开，排障不必去翻日志。
 */
export interface BrokerDiagnostics {
  host_python?: string;
  host_abi?: number | string;
  /** ABI 版本 → 随包运行时路径（空 = 该 ABI 无随包运行时 ⇒ 必然走系统解释器） */
  bundled_runtimes?: Record<string, string>;
  /** 仅 deep=1：系统 Python 运行时发现结果 */
  system_runtimes?: Record<string, string>;
  connections?: BrokerDiagConnection[];
  generated_at?: number;
}

/** 券商连接管理 API。路径与 backend/app/routes/broker.py 对应。 */
export const brokerApi = {
  profiles: () => http.get<BrokerProfile[]>("/brokers/profiles"),

  list: () => http.get<BrokerConnection[]>("/brokers"),

  /**
   * 新建连接。
   *
   * ★ 后端**幂等**：同一「券商 + 客户端路径 + 资金账号」重复提交会**复用**既有连接
   * 并返回 `reused: true`，不会新增第二条 —— 否则两条连接抢同一个 QMT userdata
   * 目录，第二条必然连不上（用户看到的就是「点击连接报错」）。调用方必须据
   * `reused` 如实告知用户，不要假装新建成功。
   */
  add: (body: {
    broker_id: string;
    client_path: string;
    account_id: string;
    account_type?: string;
    /** 客户端模式：mini（极速版，程序化通道更稳）/ full / auto */
    client_mode?: string;
    session_id?: number;
    /**
     * ★ 连接名。后端读的是 **`name`**（`routes/broker.py` 的 `body.get("name", "")`）。
     *
     * ⚠️ 此处曾写作 `label` —— 后端从不读它，于是用户在「一键连接」里看到的
     * 客户端名字被**静默丢弃**，连接列表退化成后端推断名（R25 修复）。
     */
    name?: string;
    /** ★ 显式带 conn_id = **编辑**既有连接（后端会跳过「同身份去重」，按 id 更新） */
    conn_id?: string;
    /** 后端默认 true：建连即拉起子进程握手（routes/broker.py 的 add_broker） */
    autoconnect?: boolean;
  }) => http.post<BrokerConnection & { reused?: boolean }>("/brokers", body),

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

  /**
   * 端到端可观测性快照（排障用）：宿主 ABI、随包桥接运行时、各连接状态与行情泵健康。
   *
   * `deep=1` 额外含系统 Python 运行时发现与各连接的 ABI 桥接方案
   * （首次会 spawn `py` 启动器 + 注册表扫描，较重）。
   * 这是「桥接子进程握手失败」这类问题的**自证面**：不必让用户去翻 stderr。
   */
  diagnostics: (deep = false) =>
    http.get<BrokerDiagnostics>("/brokers/diagnostics", { query: deep ? { deep: 1 } : {} }),

  launch: (connId: string) => http.post<{ ok: boolean }>("/brokers/launch", { conn_id: connId }),
};
