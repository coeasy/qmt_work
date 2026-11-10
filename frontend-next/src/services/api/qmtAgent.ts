import { http } from "../http";

/**
 * 大 QMT 策略桥 agent 部署 API（`app/routes/qmt_agent.py` 的 `/qmt-agent/*`）。
 *
 * ## 契约要点（照抄后端，改动前先对后端）
 *
 * - `GET  /qmt-agent/status`    —— 一次性给出探测结果：QMT 目录、当前 bundle、
 *   config 位置、工具链是否可用（打包态与开发态都可用）。
 * - `GET  /qmt-agent/bundle`    —— 生成当前 bundle 文本；**不写盘**。
 *   用于下载 / 复制粘贴 / 对比。
 * - `POST /qmt-agent/deploy`    —— 一键部署。幂等：目标已存在则先备份 `.bak.<epoch>`
 *   再原子替换。失败不 5xx（走 envelope 的 `code!=0`），便于界面直接展示 reason。
 * - `POST /qmt-agent/diagnose`  —— 编码 + 心跳 + 注册态三重判定。返回的 `problems[]`
 *   是**必须显式呈现**的异常，界面不要吞。若产物是**闭环** bundle，心跳改由客户端
 *   日志的 `@@QMTW|` 行推导（新规禁策略读写文件，旧的三份桥文件不会生成）。
 * - `GET  /qmt-agent/closed-loop` —— 闭环运行态（只读日志，可高频轮询）。
 * - `GET  /qmt-agent/config`    —— 读取当前 agent_config.json；未存在时回退模板
 *   （`source === "template"`）。
 * - `POST /qmt-agent/config`    —— 写入 agent_config.json（先备份原文件）。
 * - `GET  /qmt-agent/tools`     —— 调试用：列出打包进客户端的工具链文件。
 *
 * ## 零 mock 契约（对齐后端）
 *
 * - 生成 bundle 一律走后端 `gen_qmt_agent_bundle.build()`，前端**不生成**任何
 *   bundle 文本；这样「客户端下载的」与「QMT 里跑的」字节一致。
 * - `deploy` 的 `dry_run` 是显式字段，不传即默认 true；界面「预览」按钮必须走
 *   `dry_run: true`，避免误写。
 * - `problems[]` 出现即视为部署失败；界面必须红色醒目显示，不许静默。
 */

export interface QmtAgentStatus {
  qmt_dir: string | null;
  qmt_running: string[];
  agent_tools_available: boolean;
  agent_bigqmt_available: boolean;
  internal_dir: string;
  bundle_version: string;
  bundle_chars_estimate: number;
  deployed: {
    exists: boolean;
    path?: string;
    size_bytes?: number;
    mtime?: string;
  };
  config: {
    path: string;
    exists: boolean;
    bridge_dir: string | null;
  };
  checked_at: string;
}

/**
 * 产物合规画像（2026-10-23 新规）。
 *
 * ★ 与 `ok` 是**两个不同的轴**：`ok` 只说「文件写下去了」，`usable` 才说
 *   「这份产物在新规下能不能真跑」。把它们混成一个字段，界面就会出现
 *   「部署成功」的绿色提示，而盘前一个事件都收不到 —— 本仓最忌讳的假绿灯。
 */
export interface QmtAgentCompliance {
  mode: "bridge" | "closed";
  regulation_cutoff: string;
  usable: boolean;
  reason: string;
  next_action: string;
}

export interface QmtAgentBundle {
  text: string;
  encoding: string;
  size_bytes: number;
  problems: string[];
  source_agent_dir: string;
  source_tools_dir: string;
  compliance?: QmtAgentCompliance;
}

export interface QmtAgentDeployInput {
  qmt_dir?: string;
  filename?: string;
  strategy?: string;
  dry_run: boolean;
  txt_copy: boolean;
}

export interface QmtAgentDeployResult {
  ok: boolean;
  path: string;
  size_bytes: number;
  encoding: string;
  backup: string | null;
  problems: string[];
  dry_run: boolean;
  txt_path?: string;
  qmt_running: string[];
  registered_hint: string;
  /** 部署「成功」≠ 产物可用：见 `QmtAgentCompliance`。 */
  compliance?: QmtAgentCompliance;
}

export interface QmtAgentDiagnoseInput {
  qmt_dir?: string;
  strategy?: string;
}

export interface QmtAgentDiagnoseBundle {
  path: string;
  size_bytes: number;
  health?: Record<string, unknown>;
  health_error?: string;
  read_error?: string;
}

export interface QmtAgentDiagnoseHeartbeat {
  bridge_dir: string | null;
  alive: boolean;
  /** ISO 本地时间串（后端 `to_iso()`）；旧 `agent_status.json` 里可能是别的形状。 */
  ts: string | number | null;
  runtime_mode?: string;
  agent_ver?: string;
  probe_ok?: boolean;
  /** agent 自检里失败的 step 名（如 quote_call / order_funcs）。 */
  probe_bad_steps?: string[];
  /**
   * 心跳是否**可判**。`false` = 判不出来（闭环体检脚本缺失等），
   * 此时 `alive` 无意义 —— 界面必须显示「判不出来」，不许显示「未运行」。
   */
  known?: boolean;
  /** `known === false` 时的原因原文。 */
  reason?: string;
  // ---- 以下仅闭环形态提供（来自 STATUS 心跳行，与 BOOT 同源） ----
  uptime_s?: number | null;
  nav?: number | null;
  positions?: number | null;
  pending?: number | null;
  orders_sent?: number | null;
  turnover?: number | null;
  in_window?: boolean | null;
}

export interface QmtAgentDiagnoseProblem {
  source: string;
  msg: string;
}

/**
 * 闭环策略的运行态（`GET /qmt-agent/closed-loop`，也可从 `diagnose` 的
 * `closed_loop` 字段拿到）。
 *
 * ## 为什么需要它
 *
 * 2026-10-23 新规起，策略进程**禁文件读写**：闭环模式下 `agent_status.json` /
 * `probe_result.json` / `events.ndjson` 一个都不会生成。旧界面只看那三份文件，
 * 于是把一条**正在正常下单**的策略显示成「agent 未运行」—— 假红灯。
 *
 * 闭环的唯一证据链是客户端日志里的 `@@QMTW|` 行，后端已把它解析成本结构。
 *
 * ## 字段口径
 *
 * - `exit_code`：**原样**透出后端体检脚本的退出码语义
 *   （`0` 就绪 / `2` 需人工动作 / `1` 环境缺失）。界面不要自己再拼一套判断。
 * - `alive`：最后一条日志记录距今 ≤ 90s。注意它判的是**策略在打印**，
 *   不是「策略赚了钱」。
 * - `standalone`：独立进程形态。终端不会注入任何交易函数 ⇒ 一单都发不出去，
 *   这是闭环「看起来在跑、实际空转」的第一号成因。
 * - `orders` / `rejects` / `settled` / `done` / `window_closed`：**本次会话**
 *   （同一 `boot_id`）内的计数，不受上一次会话污染。
 * - `readonly` / `halted`：非空即表示策略当前不交易（原因原文）。
 */
export interface QmtAgentClosedLoopState {
  mode: "closed_loop";
  /** false 表示后端缺 `qmt_closed_loop_verify.py`，**判不出来**（不是「没问题」）。 */
  available: boolean;
  reason?: string;
  plan_id: string;
  log_path: string;
  log_error: string;
  exit_code: 0 | 1 | 2;
  /** 后端判定原文（`[OK]` / `[FAIL]` / `[i]` 前缀），与命令行体检同一句话。 */
  findings: string[];
  boot_id: string;
  alive: boolean;
  last_age_s: number | null;
  last_kind?: string;
  standalone: boolean;
  readonly: string;
  halted: string;
  orders: number;
  rejects: number;
  settled: number;
  done: boolean;
  window_closed: boolean;
  kinds: string[];
  stats: Record<string, number>;
  /** 启动快照（来自 BOOT 行）：这一轮到底想做什么。 */
  account?: string;
  window?: string[];
  targets?: number | null;
  trading_enabled?: boolean | null;
  injected?: string[];
  default_account_type?: string;
  query_account_type?: string;
  /** 心跳子对象：刻意与旧 `agent_status.json` **同形**，前端不必学第二套字段名。 */
  heartbeat?: QmtAgentDiagnoseHeartbeat;
}

export interface QmtAgentClosedLoopResponse {
  qmt_dir: string;
  strategy_dir: string;
  bundle: { path: string; size_bytes: number };
  /** `null` = 这份 bundle **不是**闭环产物（旧文件桥模式）。 */
  closed_loop: QmtAgentClosedLoopState | null;
  checked_at: string;
}

/**
 * 能力真相（agent 自检结论）。
 *
 * ★ 这是「能不能下单 / 能不能取行情」的**唯一权威答案**，不要让用户从行情报错
 *   或全绿的其它面板里去猜。
 * ★ `expected_in_mode=true` 表示该缺失由**运行模式**决定（如独立进程模式拿不到
 *   passorder），属正常形态；`false` 表示它是真故障（如行情实调失败）。
 */
export interface QmtAgentCapability {
  available: boolean;
  reason: string;
  expected_in_mode: boolean;
}

/**
 * 下单接口面明细（agent 自报，与 `capabilities.trading` 同源）。
 *
 * `capabilities.trading` 给**结论**（能不能下单），本字段给**依据**（注入进来的下单
 * 入口有哪些、还缺哪些）。此前它只落在 `probe_result.json` 里被脚本消费，界面拿不到
 * ⇒ 用户只能读到「下单能力：不可用」却无从知道缺哪个入口。
 */
export interface QmtAgentTradeSurface {
  present: string[];
  missing: string[];
  can_submit: boolean;
}

export interface QmtAgentDiagnoseResult {
  ok: boolean;
  qmt_dir: string;
  strategy_dir: string;
  bundle: QmtAgentDiagnoseBundle;
  encoding_ok: boolean;
  encoding_note: string;
  config: { path: string; exists: boolean; bridge_dir: string | null };
  heartbeat: QmtAgentDiagnoseHeartbeat;
  qmt_running: string[];
  capabilities?: { trading?: QmtAgentCapability; quote?: QmtAgentCapability };
  trade_surface?: QmtAgentTradeSurface;
  /**
   * 闭环运行态。`null` / `undefined` = 不是闭环产物（心跳来自 `agent_status.json`）。
   * 非空时 `heartbeat` **已由日志推导**，本字段给出依据。
   */
  closed_loop?: QmtAgentClosedLoopState | null;
  problems: QmtAgentDiagnoseProblem[];
}

export interface QmtAgentConfigResponse {
  path: string;
  exists: boolean;
  data: Record<string, unknown> | null;
  source?: "template";
  template_path?: string;
  hint?: string;
}

export interface QmtAgentToolsResponse {
  frozen: boolean;
  internal_dir: string;
  agent_bigqmt_dir: string;
  qmt_tools_dir: string;
  tools: { name: string; size: number }[];
  sources: { name: string; size: number }[];
}

export interface QmtAgentDistributeStatus {
  enabled: boolean;
  url: string;
  local_version: string;
  hint: string;
}

export interface QmtAgentDistributeCheckResult {
  url: string;
  local_version: string;
  remote_version: string;
  has_update: boolean;
  bundle_url?: string;
  sha256?: string;
  size_bytes?: number;
}

export interface QmtAgentDistributePullResult {
  ok: boolean;
  path: string;
  size_bytes: number;
  backup: string | null;
  dry_run: boolean;
  remote_url: string;
}

export const qmtAgentApi = {
  status: () => http.get<QmtAgentStatus>("/qmt-agent/status"),
  bundle: () => http.get<QmtAgentBundle>("/qmt-agent/bundle"),
  deploy: (body: QmtAgentDeployInput) =>
    http.post<QmtAgentDeployResult>("/qmt-agent/deploy", body),
  diagnose: (body: QmtAgentDiagnoseInput) =>
    http.post<QmtAgentDiagnoseResult>("/qmt-agent/diagnose", body),
  /**
   * 闭环运行态（只读日志，比 `diagnose` 轻得多，可定时轮询）。
   *
   * 与 `diagnose` 的分工：`diagnose` 判**产物**（编码/注册/体检），本方法判
   * **运行**（心跳、发单数、只读/停机/拒单）。
   */
  closedLoop: (opts?: { qmt_dir?: string; strategy?: string; log_path?: string }) => {
    const q: Record<string, string> = {};
    if (opts?.qmt_dir) q.qmt_dir = opts.qmt_dir;
    if (opts?.strategy) q.strategy = opts.strategy;
    if (opts?.log_path) q.log_path = opts.log_path;
    return http.get<QmtAgentClosedLoopResponse>("/qmt-agent/closed-loop", {
      query: Object.keys(q).length ? q : undefined,
    });
  },
  getConfig: (qmt_dir?: string) =>
    http.get<QmtAgentConfigResponse>("/qmt-agent/config", {
      query: qmt_dir ? { qmt_dir } : undefined,
    }),
  saveConfig: (config: Record<string, unknown>, qmt_dir?: string) =>
    http.post<{ path: string; written: number; encoding: string }>("/qmt-agent/config", {
      config,
      qmt_dir,
    }),
  tools: () => http.get<QmtAgentToolsResponse>("/qmt-agent/tools"),
  distributeStatus: () =>
    http.get<QmtAgentDistributeStatus>("/qmt-agent/distribute/status"),
  distributeCheck: (url?: string) =>
    http.post<QmtAgentDistributeCheckResult>("/qmt-agent/distribute/check", undefined, {
      query: url ? { url } : undefined,
    }),
  distributePull: (opts: {
    url?: string;
    filename?: string;
    qmt_dir?: string;
    dry_run?: boolean;
  }) => {
    const q: Record<string, string> = {};
    if (opts.url) q.url = opts.url;
    if (opts.filename) q.filename = opts.filename;
    if (opts.qmt_dir) q.qmt_dir = opts.qmt_dir;
    if (opts.dry_run !== undefined) q.dry_run = String(opts.dry_run);
    return http.post<QmtAgentDistributePullResult>("/qmt-agent/distribute/pull", undefined, {
      query: q,
    });
  },
};
