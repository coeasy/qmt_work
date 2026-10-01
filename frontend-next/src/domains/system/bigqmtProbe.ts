import type { BigQmtProbe } from "@/services/api";

/**
 * 大 QMT 探针 → 界面可展示的结论（**纯函数**，无 React 依赖）。
 *
 * 为什么单独抽出来
 * ----------------
 * 大小 QMT 的差异**不在「能不能连」**，而在「**哪一层断了**」。六种故障在界面上
 * 的表现完全一样 —— 「连接成功但没有数据」：
 *
 *   ① agent 没运行（策略没在终端里跑）
 *   ② bridge_dir 两端不一致（请求写进了另一个目录，永远等不到响应）
 *   ③ 注入函数缺失（终端版本不提供这些函数）
 *   ④ trading_enabled=false（只读可用，下单被 agent 拒绝）
 *   ⑤ 行情泵没跑（`pump_running=false`，界面永远空白）
 *   ⑥ 事件泵没跑（成交回报永远到不了前端）
 *   ⑦ **行情订阅没下发**（agent 不知道要转发哪些标的 ⇒ 盘口价格永远停在
 *      订阅那一刻的种子值，而连接、健康、日志全是绿的）
 *
 * 把它们压成一句话结论是这一层唯一的职责。抽成纯函数是为了能**直接断言**这些
 * 分流，而不是靠渲染整个诊断页去间接验证。
 *
 * ★ 根因结论由**后端**给出（`probe.root_cause`），前端只负责兜底与展示：
 *   路径比较涉及 Windows 的大小写 / 正反斜杠 / `\\?\` 长路径前缀，
 *   前端重算一遍必然与后端漂移。`dirTone` 因此也严格跟随 `bridge_dir_match`。
 */

export interface BigQmtProbeView {
  /** 传输形态（file / redis / zmq / inprocess） */
  transport: string;
  /** 事件语义（PUSH / POLL_DIFF / PUSH_WITH_GAP / NONE） */
  eventSemantics: string;
  /** 事件延迟上界文案（无上界时为空串） */
  latencyLabel: string;
  /** 后端给出的单一根因结论；空串 = 未发现问题 */
  rootCause: string;
  /** agent 状态一行摘要 */
  headline: string;
  /** 桥目录一致性文案 */
  dirLabel: string;
  /**
   * 桥目录一致性色调。
   * `unknown` 与 `bad` 必须分开：把「还不知道」显示成「不一致」会让用户
   * 白折腾一次改配置（agent 只是还没来得及回报元数据）。
   */
  dirTone: "ok" | "warn" | "unknown";
  /** 两端 op 数量对不上 = 契约漂移 */
  opsDrift: boolean;
  /** 能力面对账文案 */
  capabilityLine: string;
  /** 事件泵是否在跑（与「握手成功」是两件事） */
  pumpOk: boolean;
  pumpLabel: string;
  funcsCount: number;
  subscribed: number;
  callbackBound: boolean;
  /** 方向仲裁落 unknown 次数（>0 说明方向字段不可信，不得用于风控判定） */
  directionUnknown: number;
  clockOffsetMs: number;
  localBridgeDir: string;
  agentBridgeDir: string;
  /** 本端期望 agent 转发的标的数 */
  quoteWanted: number;
  /** 已确认下发成功的标的数 */
  quoteApplied: number;
  /**
   * 行情订阅状态色调。
   * `unknown` 用于「本端还没订阅任何标的」——此时无从判断，不能报绿也不能报红。
   */
  quoteTone: "ok" | "warn" | "unknown";
  /** 行情订阅状态一行文案 */
  quoteLabel: string;
  /**
   * 存活判定：**「现在可用吗」**（而不是「曾经连上过吗」）。
   *
   * ★ 为什么必须单列一行而不是复用 `pumpOk` / `connected`：文件桥没有「连接」，
   *   agent 被停止 / QMT 被关掉之后，旧的 `_connected` 标志**永远是真**，
   *   前端一直显示「已连接」、健康检查恒判 ok、重连永远不会被调度。
   *   后端的 `available` / `agent_unresponsive` 才是诚实答案。
   */
  livenessTone: "ok" | "warn" | "unknown";
  /** 存活状态一行文案 */
  livenessLabel: string;
  /** 后端 `is_connected()` 当前结论 */
  available: boolean;
  /** 距上一次成功往返多少秒；null = 从未成功过 */
  lastAgentOkAgeS: number | null;
}

export function describeBigQmtProbe(probe: BigQmtProbe | undefined | null): BigQmtProbeView {
  const p = probe ?? {};
  const agent = p.agent ?? {};
  const hasAgent = Object.keys(agent).length > 0;

  const opsLocal = p.supported_ops?.length ?? 0;
  const opsRemote = p.actions?.length ?? agent.actions?.length ?? 0;
  // 两端都是 0 时无法判定（探测未发生），不算漂移 —— 否则首次渲染就报红。
  const opsDrift = opsLocal > 0 && opsRemote > 0 && opsLocal !== opsRemote;

  const dirMatch = p.bridge_dir_match;
  // ★ agent 从未应答过 ⇒ 无论 bridge_dir_match 传了什么，一律判「待确认」。
  //   否则一旦上游给了 true（例如字段缺省、或上一次探测的陈旧缓存），界面会显示
  //   「桥目录一致」——而 agent 其实根本没跑。这正是本项目反复栽过的
  //   「绿灯被另一个 bug 遮出来」：用户看到一致就不查了。
  const dirTone: BigQmtProbeView["dirTone"] = !hasAgent
    ? "unknown"
    : dirMatch === true ? "ok" : dirMatch === false ? "warn" : "unknown";
  const dirLabel = dirTone === "ok" ? "桥目录一致"
    : dirTone === "warn" ? "桥目录不一致"
      : "桥目录待确认";

  const funcsCount = agent.funcs?.length ?? 0;
  const subscribed = p.subscribed ?? agent.subscribed ?? 0;
  const callbackBound = p.callback_bound ?? agent.callback_bound ?? false;
  const directionUnknown = p.direction_unknown ?? agent.direction_unknown ?? 0;
  const clockOffsetMs = p.clock_offset_ms ?? 0;
  const pumpOk = p.pump_running === true;

  // ---- 行情订阅对账面（第 ⑦ 类故障：agent 不知道要转发什么）----
  // 三个数各说一件事，缺一不可：
  //   quoteWanted  = 本端**想要**什么（订阅意图）
  //   quoteApplied = 本端**确认下发成功**了什么（真正的判据）
  //   subscribed   = **agent 自报**收到了什么（可能滞后一拍，仅供参考）
  // 只看 subscribed 会误判：它是最近一次响应信封的缓存，事件泵只读文件不发请求，
  // 缓存可能长期不刷新 ⇒ 「agent 说 0」并不等于「没下发」。
  const quoteWanted = p.quote_wanted ?? 0;
  const quoteApplied = p.quote_applied ?? 0;
  const quoteErr = p.quote_sync_error ?? "";
  let quoteTone: BigQmtProbeView["quoteTone"] = "ok";
  let quoteLabel = "";
  if (quoteWanted <= 0) {
    quoteTone = "unknown";
    quoteLabel = "本端尚未订阅任何标的";
  } else if (quoteErr) {
    quoteTone = "warn";
    quoteLabel = `订阅下发失败（待在窗口后重试）：${quoteErr}`;
  } else if (quoteApplied < quoteWanted) {
    quoteTone = "warn";
    quoteLabel = `订阅未下发 ${quoteWanted - quoteApplied}/${quoteWanted}`
      + "（等下一次对账；长时间不变请查 agent 是否在跑）";
  } else {
    // 下发成功 ≠ 行情在推：事件泵没跑时价格同样不动，这里必须点出来，
    // 否则用户会以为「订阅没问题」而漏掉第 ⑤ 类故障。
    quoteLabel = `已下发给 agent ${quoteApplied}/${quoteWanted}`
      + (pumpOk ? "" : "（但事件泵未运行，行情不会刷新）");
  }
  // subscribed 只能作为**辅助**判据，且必须限定在 0 < subscribed < applied：
  //   · subscribed === 0 是「刚订阅完、缓存还没刷新」的**正常**状态（agent_meta
  //     是最近一次响应信封的缓存），据此报红属于误报 —— 宁可漏标不可误标；
  //   · subscribed 介于两者之间才说明缓存足够新、而 agent 确实少收了
  //     （典型：agent 重启后只恢复了部分，等待本端重发）。
  if (quoteTone === "ok" && subscribed > 0 && subscribed < quoteApplied) {
    quoteTone = "warn";
    quoteLabel += `；agent 自报仅 ${subscribed} 个，可能已重启（等待重发）`;
  }

  // ---- 存活面（第 ⑧ 类故障：agent 已经死了，界面却说「已连接」）----
  // 三个量的分工：
  //   available          = 后端 `is_connected()` 的当前结论（「现在可用」）—— 判据
  //   agent_unresponsive = **为什么**不可用（连续探活无应答）
  //   last_agent_ok_age_s= 「上次可用」是多久以前（null = 从未可用过）
  // ★ `unknown` 与 `warn` 必须分开：agent 从未应答过时无从判断可用性，报红会在
  //   用户刚打开面板、或本来就没启动策略时制造一片假红。
  const available = p.available === true;
  const unresponsive = p.agent_unresponsive === true;
  const livenessFailures = p.liveness_failures ?? 0;
  const ageS = typeof p.last_agent_ok_age_s === "number" ? p.last_agent_ok_age_s : null;
  let livenessTone: BigQmtProbeView["livenessTone"];
  let livenessLabel: string;
  if (unresponsive) {
    livenessTone = "warn";
    livenessLabel = `探活连续 ${livenessFailures} 次无应答 ⇒ 判定**不可用**`
      + (ageS !== null
        ? `（曾经可用，最近一次成功往返在 ${ageS}s 前 —— agent 可能已被停止或 QMT 已关闭）`
        : "");
  } else if (available) {
    livenessTone = "ok";
    livenessLabel = ageS !== null
      ? `现在可用（最近一次成功往返 ${ageS}s 前）`
      : "现在可用";
  } else if (!hasAgent) {
    livenessTone = "unknown";
    livenessLabel = "agent 未应答，可用性未知";
  } else {
    livenessTone = "unknown";
    livenessLabel = "未连接（尚无一次成功往返）";
  }

  const headline = hasAgent
    ? `agent v${agent.ver ?? "?"} / py${agent.py ?? "?"} · 注入函数 ${funcsCount} 个 · `
      + `${agent.trading_enabled ? "可下单" : "只读（trading_enabled=false）"} · 已订阅 ${subscribed}`
    : "agent 未应答";

  const parts = [`本端 ${opsLocal} 项`, `agent ${opsRemote} 项`];
  if (opsDrift) parts.push("数量不一致＝契约漂移，请核对两侧 op 清单");

  return {
    transport: p.transport || "—",
    eventSemantics: p.event_semantics || "—",
    latencyLabel: p.max_latency_ms ? `（上界 ${p.max_latency_ms}ms）` : "",
    rootCause: p.root_cause ?? "",
    headline,
    dirLabel,
    dirTone,
    opsDrift,
    capabilityLine: parts.join(" / "),
    pumpOk,
    pumpLabel: pumpOk ? "运行中" : "未运行",
    funcsCount,
    subscribed,
    callbackBound,
    directionUnknown,
    clockOffsetMs,
    localBridgeDir: p.local_bridge_dir ?? "",
    agentBridgeDir: p.agent_bridge_dir ?? "",
    quoteWanted,
    quoteApplied,
    quoteTone,
    quoteLabel,
    livenessTone,
    livenessLabel,
    available,
    lastAgentOkAgeS: ageS,
  };
}

/**
 * 探针的「求救提示」：根因存在时给一句**可照做**的引导。
 *
 * 与后端 `root_cause` 的区别：后端结论是事实，这里负责补充「界面层还能看什么」——
 * 例如桥目录不一致时，必须把两个目录都摊出来，否则用户不知道该抄哪个。
 */
export function bigQmtProbeHint(view: BigQmtProbeView): string {
  if (view.rootCause) {
    if (view.dirTone === "warn" && view.localBridgeDir && view.agentBridgeDir) {
      return `${view.rootCause}（本机：${view.localBridgeDir}；agent：${view.agentBridgeDir}）`;
    }
    return view.rootCause;
  }
  // 根因面没报问题、行情订阅面却报了 —— 必须说出来：否则提示行为空，
  // 用户会以为「一切正常」，从而漏掉第 ⑦ 类故障（连上了却没有任何行情刷新）。
  // 存活面报了「不可用」—— 同样必须说出来。后端的 root_cause 覆盖的是**配置/能力**
  // 类的四类根因；而「agent 进程已经没了」是**进程**层面的事，不一定进那四类判定。
  // 不在这里兜底，提示行会为空，用户就会以为「一切正常」。
  if (view.livenessTone === "warn") return view.livenessLabel;
  if (view.quoteTone === "warn") return view.quoteLabel;
  return "";
}
