import { http } from "../http";
import type { BrokerConnection, BrokerProfile } from "@/shared/types";

export interface BrokerTestResult {
  ok: boolean;
  runtime_mode?: "inproc" | "bridge" | "bigqmt_bridge" | string;
  reason?: string;
  runtime_source?: string;
  suggestions?: string[];
  /** 桥接探测附加面（connector_probe）：transport / agent 元数据 / 事件语义 */
  connector_key?: string;
  transport?: string;
  agent?: Record<string, unknown>;
  event_semantics?: string;
  max_latency_ms?: number;
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

/**
 * 大 QMT 桥的探针面（后端 `BigQmtBridge.connector_probe()`）。
 *
 * 字段与后端**逐一对齐**，不做可选化猜测 —— 这一面存在的唯一目的就是排障，
 * 猜出来的字段名会让「诊断面板显示了但读的是不存在的东西」这种假绿灯再现。
 *
 * 排障四分法（后端已把结论算进 `root_cause`，前端**不要**自己重新判定）：
 *   ① agent 未运行          → `agent` 为空
 *   ② bridge_dir 两端不一致 → `bridge_dir_match === false`
 *   ③ 注入函数缺失          → `agent.funcs` 为空
 *   ④ token 不匹配          → 能收到响应即已排除该可能
 */
export interface BigQmtProbe {
  /** 连接器 key，如 qmt.big.bridge.file */
  connector_key?: string;
  /** file / redis / zmq */
  transport?: string;
  /** 本连接器能翻译的 canonical op 清单（由 dialect 自报） */
  supported_ops?: string[];
  /** agent 上报的元数据（进程内 Python 版本、注入函数、桥目录、订阅数…） */
  agent?: {
    ver?: string;
    py?: string;
    funcs?: string[];
    trading_enabled?: boolean;
    bridge_dir?: string;
    uptime_s?: number;
    /** agent 侧已实现的 wire action 清单；与本端 supported_ops 数量对不上＝契约漂移 */
    actions?: string[];
    /** 回调是否**真的**被终端调用过（空定义 ≠ 可用） */
    callback_bound?: boolean;
    callback_hits?: number;
    /** 买卖方向仲裁落 unknown 的次数（>0 说明方向字段不可信，勿用于风控） */
    direction_unknown?: number;
    subscribed?: number;
    ascii_only?: boolean;
  };
  /** agent 回报的桥目录（与本机配置比对用；比对结论见 bridge_dir_match） */
  agent_bridge_dir?: string;
  /** 本机配置的桥目录 */
  local_bridge_dir?: string;
  /** 两端是否同一目录；null = agent 从未回报（**未知**，不是「不一致」） */
  bridge_dir_match?: boolean | null;
  /** 后端算好的单一根因结论；空串 = 未发现问题 */
  root_cause?: string;
  /** agent 已实现的 action 清单（= agent.actions 的上浮副本） */
  actions?: string[];
  callback_bound?: boolean;
  direction_unknown?: number;
  subscribed?: number;
  ascii_only?: boolean;
  /** 两端时钟偏移（ms）：写请求的 ttl 判定依赖它，偏移过大会误拒 */
  clock_offset_ms?: number;
  /** 事件泵是否在跑 —— 与「握手成功」是两件事 */
  pump_running?: boolean;
  // ---- 存活面：区分「曾经连上」与「现在可用」----
  // ★ 后端刻意把这几个**原始量**一并上浮，而不是只给一个 connected 布尔：文件桥
  //   没有「连接」可言（它只是两个目录），唯一的诚实判据是一次真实请求/应答往返。
  //   前端若只显示「已连接」，用户就问不出「**现在**还可用吗」——而 agent 死掉后
  //   旧实现里那个标志会永远是真（历史 bug，见 TD/`BIG_QMT_COMPAT_PLAN.md` §9.6.2）。
  /** 连续探活无应答次数（任何一次成功往返即清零） */
  liveness_failures?: number;
  /** agent 连续无应答 ⇒ 后端 `is_connected()` 已据此返回 false */
  agent_unresponsive?: boolean;
  /** 距上一次**成功往返**多少秒；`null` = 从未成功过（「从未」≠「掉线」） */
  last_agent_ok_age_s?: number | null;
  /** 后端 `is_connected()` 的当前结论（「现在可用」）—— 界面以它为准 */
  available?: boolean;
  /** 本端期望 agent 转发行情的标的数（意图） */
  quote_wanted?: number;
  /** 已**确认下发成功**给 agent 的标的数；长期小于 quote_wanted = 订阅未生效 */
  quote_applied?: number;
  /** agent 自报的已订阅数（最近一次响应信封的缓存，可能滞后一拍） */
  quote_agent_subscribed?: number;
  /** 最近一次订阅下发失败的原因；空串 = 未失败 */
  quote_sync_error?: string;
  /** 事件语义（PUSH / POLL_DIFF / PUSH_WITH_GAP / NONE） */
  event_semantics?: string;
  /** 事件延迟上界（ms）；超时守护据此撤单，缺失会让守护误撤 */
  max_latency_ms?: number;
  /** 探针自身失败时的兜底（后端保证诊断附差不影响健康主结论） */
  error?: string;
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
  /** 大 QMT 桥连接：探针面（transport/agent/事件语义与延迟上界） */
  bigqmt?: BigQmtProbe;
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
    /**
     * 大 QMT 连接器组合键（路径 B）。空 = 走 xtquant 直连（mini/full 极速/完整版）。
     * 合法值由后端校验（如 qmt.big.bridge.file / .redis / .zmq）。
     */
    connector_key?: string;
    /** 桥接参数：file=桥目录（须与大 QMT 端 agent_config.json 完全一致）；redis=连接串；zmq=tcp 地址 */
    bridge_dir?: string;
    /** 桥鉴权 token（须与大 QMT 端一致；留空=不校验，仅调试） */
    auth_token?: string;
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

  /**
   * 连接一条券商连接。
   *
   * ★ 必须显式给足超时。后端一次 connect 最坏要跑「桥接子进程冷启动 +
   *   候选数据目录(≤2) × session(6) 逐个 XtQuantTrader.connect()」，实测约 45s
   *   （`XTPQuantAdapter._CONNECT_RETRY_BUDGET`），加子进程启动余量接近 60s。
   *   前端默认 15s 会在**诊断信息产出之前**就 abort 掉请求，用户只看到
   *   「请求超时（15s）」——后端辛苦构造的根因（客户端日志证据 + 官方四步排查）
   *   永远到不了界面。这正是「前后端未贯通」的典型形态，故此处显式覆盖。
   *   120s = 后端最坏 ~60s 的两倍余量（含磁盘 / 客户端日志 IO 抖动）。
   */
  connect: (connId: string) =>
    http.post<{ ok: boolean; reason?: string }>(
      `/brokers/${connId}/connect`, undefined, { timeout: 120_000 }),

  disconnect: (connId: string) =>
    http.post<{ ok: boolean }>(`/brokers/${connId}/disconnect`),

  setActive: (connId: string) => http.post<{ ok: boolean }>(`/brokers/${connId}/active`),

  /** 环境探测：与 connect 同源（都要跑适配器 start()），超时同样必须给足。
   * 带 connector_key 时后端走桥接一次性 PROBE 分支（不落库）。 */
  test: (body: {
    broker_id: string;
    client_path: string;
    connector_key?: string;
    bridge_dir?: string;
    auth_token?: string;
    account_id?: string;
    account_type?: string;
  }) =>
    http.post<BrokerTestResult>("/brokers/test", body, { timeout: 120_000 }),

  /**
   * 自动发现本机 QMT / MiniQMT 客户端（运行中进程 + 安装目录扫描）。
   * 只读、无副作用；候选里已含自动读出的资金账号，可免手填。
   *
   * 实测本机全盘扫描 + 逐个探 xtquant 可导入性约 25s，默认 15s 会把「正在发现」
   * 误报成「发现失败」；`retry: 0` 避免把 25s 级的扫描白跑两遍。
   */
  autoDetect: () =>
    http.get<AutoDetectResult>("/brokers/auto-detect", { timeout: 60_000, retry: 0 }),

  runtimes: () => http.get<Record<string, unknown>>("/brokers/runtimes"),

  /** 版本画像：内部会做一次客户端目录探测（可能 spawn 子进程），同样给足超时。 */
  versionInfo: (body: { client_path: string }) =>
    http.post<VersionProfile>("/brokers/version-info", body, { timeout: 60_000 }),

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
