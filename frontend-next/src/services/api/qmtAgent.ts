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
 *   是**必须显式呈现**的异常，界面不要吞。
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

export interface QmtAgentBundle {
  text: string;
  encoding: string;
  size_bytes: number;
  problems: string[];
  source_agent_dir: string;
  source_tools_dir: string;
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
  ts: number | null;
  runtime_mode?: string;
  agent_ver?: string;
  probe_ok?: boolean;
  /** agent 自检里失败的 step 名（如 quote_call / order_funcs）。 */
  probe_bad_steps?: string[];
}

export interface QmtAgentDiagnoseProblem {
  source: string;
  msg: string;
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
