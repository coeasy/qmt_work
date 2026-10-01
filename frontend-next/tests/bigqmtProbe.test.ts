import { describe, expect, it } from "vitest";
import { bigQmtProbeHint, describeBigQmtProbe } from "@/domains/system/bigqmtProbe";

/**
 * 大 QMT 探针面板的**判定**回归测试（纯函数，不渲染）。
 *
 * 锁的是「六种故障必须互相可区分」这条约束。它们在实际使用中的界面表现**完全
 * 一样** —— 「连接成功了，但什么都看不到」：
 *
 *   ① agent 没运行    ② bridge_dir 两端不一致   ③ 注入函数缺失
 *   ④ trading_enabled=false   ⑤ 行情泵没跑   ⑥ 事件泵没跑
 *
 * 这六种情况的**修复动作完全不同**（去终端跑策略 / 改配置路径 / 换终端版本 /
 * 改 agent_config / 查 pump / 查事件端口）。压成同一个「未连接」等于没有诊断。
 */

/** 一份「一切正常」的探针输出，各用例只覆盖需要变化的那一两个字段。 */
const healthy = {
  connector_key: "qmt.big.bridge.file",
  transport: "file",
  supported_ops: ["PLACE_ORDER", "CANCEL_ORDER", "GET_POSITIONS", "GET_KLINE"],
  actions: ["PLACE", "CANCEL_ORDER", "QUERY_POSITION", "QUERY_KLINE"],
  agent: {
    ver: "1.0.0",
    py: "3.6.8",
    funcs: ["passorder", "cancel", "get_trade_detail_data", "get_trading_dates"],
    trading_enabled: true,
    bridge_dir: "C:/qmt/bridge",
    actions: ["PLACE", "CANCEL_ORDER", "QUERY_POSITION", "QUERY_KLINE"],
    callback_bound: true,
    direction_unknown: 0,
    subscribed: 3,
  },
  agent_bridge_dir: "C:/qmt/bridge",
  local_bridge_dir: "C:/qmt/bridge",
  bridge_dir_match: true,
  root_cause: "",
  clock_offset_ms: 2,
  pump_running: true,
  event_semantics: "POLL_DIFF",
  max_latency_ms: 1500,
  // 存活面：健康态 = 「现在可用」，且最近一次成功往返是 12s 前。
  available: true,
  agent_unresponsive: false,
  liveness_failures: 0,
  last_agent_ok_age_s: 12,
};

describe("describeBigQmtProbe · 健康态", () => {
  it("正常连接：无根因、两个泵都在跑、无契约漂移", () => {
    const v = describeBigQmtProbe(healthy);
    expect(v.rootCause).toBe("");
    expect(v.pumpOk).toBe(true);
    expect(v.opsDrift).toBe(false);
    expect(v.dirTone).toBe("ok");
    expect(v.dirLabel).toBe("桥目录一致");
    expect(v.funcsCount).toBe(4);
    expect(v.subscribed).toBe(3);
    expect(v.callbackBound).toBe(true);
    expect(v.headline).toContain("可下单");
    expect(v.livenessTone).toBe("ok");
    expect(v.available).toBe(true);
    expect(v.livenessLabel).toContain("现在可用");
  });

  it("event_semantics / max_latency_ms 必须原样透出（超时守护据此撤单）", () => {
    const v = describeBigQmtProbe(healthy);
    expect(v.eventSemantics).toBe("POLL_DIFF");
    expect(v.latencyLabel).toContain("1500");
  });

  it("空入参不崩、不谎报（用于首次渲染 / 接口异常）", () => {
    const v = describeBigQmtProbe(undefined);
    expect(v.transport).toBe("—");
    expect(v.rootCause).toBe("");
    expect(v.pumpOk).toBe(false);
    expect(v.funcsCount).toBe(0);
    expect(v.opsDrift).toBe(false);
  });
});

describe("describeBigQmtProbe · 四种根因必须互相可区分", () => {
  it("① agent 没运行：headline 明说未应答，且不得与「配置错」共用文案", () => {
    const v = describeBigQmtProbe({ ...healthy, agent: {}, actions: [],
      root_cause: "agent 未运行或从未应答（桥目录里没有任何响应信封）" });
    expect(v.headline).toBe("agent 未应答");
    expect(v.funcsCount).toBe(0);
    expect(v.rootCause).toContain("未运行");
    // 未收到过元数据 ⇒ 桥目录只能判「待确认」，不能武断说不一致
    expect(v.dirTone).toBe("unknown");
    expect(v.dirLabel).toBe("桥目录待确认");
  });

  it("② bridge_dir 两端不一致：色调必须是 warn（可操作），且两个目录都摊出来", () => {
    const probe = {
      ...healthy,
      agent_bridge_dir: "D:/other/bridge",
      local_bridge_dir: "C:/qmt/bridge",
      bridge_dir_match: false as const,
      root_cause: "bridge_dir 两端不一致：agent 读的是另一个目录，请求永远等不到响应。",
    };
    const v = describeBigQmtProbe(probe);
    expect(v.dirTone).toBe("warn");
    expect(v.dirLabel).toBe("桥目录不一致");
    const hint = bigQmtProbeHint(v);
    // 用户必须能同时看到两个路径，否则不知道该抄哪一个
    expect(hint).toContain("C:/qmt/bridge");
    expect(hint).toContain("D:/other/bridge");
  });

  it("★ bridge_dir_match=null（未知）绝不能显示成「不一致」", () => {
    const v = describeBigQmtProbe({ ...healthy, bridge_dir_match: null });
    expect(v.dirTone).toBe("unknown");
    expect(v.dirLabel).not.toContain("不一致");
  });

  it("③ 注入函数缺失：funcs 数归零，与「agent 没运行」靠 headline 区分", () => {
    const v = describeBigQmtProbe({
      ...healthy,
      agent: { ...healthy.agent, funcs: [] },
      root_cause: "agent 已就绪但一个注入函数都没捕获到：策略运行在错误的位置",
    });
    expect(v.funcsCount).toBe(0);
    expect(v.headline).not.toBe("agent 未应答");   // agent 其实活着
    expect(v.headline).toContain("注入函数 0");
    expect(v.rootCause).toContain("注入函数");
  });

  it("④ trading_enabled=false：必须明说只读，不能让用户以为是连接问题", () => {
    const v = describeBigQmtProbe({
      ...healthy, agent: { ...healthy.agent, trading_enabled: false } });
    expect(v.headline).toContain("只读");
    expect(v.headline).toContain("trading_enabled=false");
  });

  it("⑤ 行情泵没跑：pumpOk=false 且文案是「未运行」", () => {
    const v = describeBigQmtProbe({ ...healthy, pump_running: false });
    expect(v.pumpOk).toBe(false);
    expect(v.pumpLabel).toBe("未运行");
  });

  it("⑤/⑥ 两个泵是独立失败点：行情泵正常时也不能连带结论", () => {
    const paused = describeBigQmtProbe({ ...healthy, pump_running: false });
    const running = describeBigQmtProbe(healthy);
    expect(paused.eventSemantics).toBe(running.eventSemantics);
    expect(paused.rootCause).toBe(running.rootCause);
    expect(paused.pumpOk).not.toBe(running.pumpOk);
  });
});

describe("describeBigQmtProbe · 契约漂移与风险字段", () => {
  it("两端 op 数量不一致 ⇒ opsDrift=true（本端能翻译 / agent 能执行对不上）", () => {
    const v = describeBigQmtProbe({
      ...healthy,
      supported_ops: ["A", "B", "C"],
      actions: ["A", "B"],
    });
    expect(v.opsDrift).toBe(true);
    expect(v.capabilityLine).toContain("契约漂移");
  });

  it("★ 一端为 0（探测未发生）不算漂移 —— 否则首屏就报红", () => {
    expect(describeBigQmtProbe({ ...healthy, supported_ops: [] }).opsDrift).toBe(false);
    expect(describeBigQmtProbe({ ...healthy, actions: [] }).opsDrift).toBe(false);
  });

  it("数量一致 ⇒ 不报漂移", () => {
    const v = describeBigQmtProbe({
      ...healthy, supported_ops: ["A", "B"], actions: ["X", "Y"] });
    expect(v.opsDrift).toBe(false);
  });

  it("direction_unknown > 0 必须上浮（方向字段不可信，不得用于风控判定）", () => {
    const v = describeBigQmtProbe({
      ...healthy, agent: { ...healthy.agent, direction_unknown: 7 } });
    expect(v.directionUnknown).toBe(7);
  });

  it("顶层字段优先于嵌套 agent 字段（后端上浮副本是真源）", () => {
    const v = describeBigQmtProbe({
      ...healthy, subscribed: 9, callback_bound: false, direction_unknown: 1,
      agent: { ...healthy.agent, subscribed: 3, callback_bound: true, direction_unknown: 0 },
    });
    expect(v.subscribed).toBe(9);
    expect(v.callbackBound).toBe(false);
    expect(v.directionUnknown).toBe(1);
  });

  it("探针自身报错时由调用方处理（本函数不吞 error）", () => {
    expect(describeBigQmtProbe({ error: "boom" }).rootCause).toBe("");
  });
});

describe("bigQmtProbeHint", () => {
  it("无根因时返回空串（界面不显示「根因：」空行）", () => {
    expect(bigQmtProbeHint(describeBigQmtProbe(healthy))).toBe("");
  });

  it("桥目录不一致时补上两个目录；其它根因原样返回", () => {
    const dirHint = bigQmtProbeHint(describeBigQmtProbe({
      ...healthy, bridge_dir_match: false,
      local_bridge_dir: "C:/a", agent_bridge_dir: "C:/b",
      root_cause: "bridge_dir 两端不一致",
    }));
    expect(dirHint).toContain("C:/a");
    expect(dirHint).toContain("C:/b");

    const plain = bigQmtProbeHint(describeBigQmtProbe({
      ...healthy, root_cause: "agent 未运行或从未应答",
    }));
    expect(plain).toBe("agent 未运行或从未应答");
  });

  it("无根因但行情订阅未下发时**不得**返回空串（否则第 ⑦ 类故障无提示）", () => {
    const v = describeBigQmtProbe({
      ...healthy, root_cause: "", quote_wanted: 3, quote_applied: 0 });
    expect(v.quoteTone).toBe("warn");
    expect(bigQmtProbeHint(v)).not.toBe("");
    expect(bigQmtProbeHint(v)).toContain("未下发");
  });
});

/**
 * 第 ⑦ 类故障：**行情订阅没下发到 agent**。
 *
 * 界面表现与前面六类完全一样 —— 「连上了，价格却是死的」。区别在于它连
 * `root_cause` 都是空的（连接完全正常），只能靠对账面看出来。
 */
describe("describeBigQmtProbe — 行情订阅对账", () => {
  it("本端尚未订阅任何标的 ⇒ unknown（无从判断，不报绿也不报红）", () => {
    const v = describeBigQmtProbe({ ...healthy, quote_wanted: 0, quote_applied: 0 });
    expect(v.quoteTone).toBe("unknown");
    expect(v.quoteLabel).toContain("尚未订阅");
  });

  it("已下发数 < 意图数 ⇒ warn，并给出还差几个", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 3, quote_applied: 1 });
    expect(v.quoteTone).toBe("warn");
    expect(v.quoteLabel).toContain("2/3");
  });

  it("下发失败 ⇒ warn，且原因必须原样带出来", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 2, quote_applied: 0,
      quote_sync_error: "TransportError: SUB_QUOTE 超时" });
    expect(v.quoteTone).toBe("warn");
    expect(v.quoteLabel).toContain("SUB_QUOTE 超时");
  });

  it("全部下发成功且事件泵在跑 ⇒ ok", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 3, quote_applied: 3, quote_sync_error: "",
      subscribed: 3, pump_running: true });
    expect(v.quoteTone).toBe("ok");
    expect(v.quoteLabel).toContain("3/3");
  });

  it("下发成功但事件泵没跑 ⇒ 仍须点明「行情不会刷新」（第 ⑤ 类故障）", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 3, quote_applied: 3, subscribed: 3,
      pump_running: false });
    expect(v.quoteTone).toBe("ok");
    expect(v.quoteLabel).toContain("事件泵未运行");
  });

  it("agent 自报数少于已下发数（但非 0）⇒ warn（多半是 agent 重启，等待重发）", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 3, quote_applied: 3, subscribed: 1 });
    expect(v.quoteTone).toBe("warn");
    expect(v.quoteLabel).toContain("重启");
  });

  it("agent 自报 0 不报红（刚订阅完缓存还没刷新，据此报红是误报）", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 3, quote_applied: 3, subscribed: 0 });
    expect(v.quoteTone).toBe("ok");
  });

  it("agent 自报数大于已下发数不报红（缓存滞后一拍，不是故障）", () => {
    const v = describeBigQmtProbe({
      ...healthy, quote_wanted: 2, quote_applied: 2, subscribed: 5 });
    expect(v.quoteTone).toBe("ok");
  });
});

/**
 * 第 ⑧ 类故障：**agent 已经死了，界面却还说「已连接」**。
 *
 * 文件桥没有「连接」可言（它只是两个目录），旧实现里的 `_connected` 标志只在
 * `start()` 置真、只在 `stop()/close()` 置假 —— agent 被停止 / QMT 被关掉之后
 * 它**永远是真**：界面一直显示「已连接」、健康检查恒判 ok、重连永远不会被调度。
 *
 * 后端的诚实判据是 `available`（当前是否可用）+ `agent_unresponsive`（为什么不可用）
 * + `last_agent_ok_age_s`（上次可用是多久以前）。三者在界面上必须能区分：
 *   · `unknown`（agent 从未应答 / 尚未建立往返）**不能**报红，否则刚打开面板就一片红；
 *   · `warn`（探活连续无应答）必须报红，且必须同时也进 `bigQmtProbeHint` —— 否则
 *     提示行为空，用户会以为「一切正常」。
 */
describe("describeBigQmtProbe — 存活判据（现在可用 vs 曾经连上）", () => {
  it("探活连续无应答 ⇒ warn，且区分「曾经可用」与「从未可用」", () => {
    const v = describeBigQmtProbe({
      ...healthy, available: false, agent_unresponsive: true, liveness_failures: 2,
      last_agent_ok_age_s: 240 });
    expect(v.livenessTone).toBe("warn");
    expect(v.livenessLabel).toContain("不可用");
    expect(v.livenessLabel).toContain("2 次");
    // 「曾经可用」必须带出来：否则用户会去查配置，而真相是 agent 进程没了。
    expect(v.livenessLabel).toContain("曾经可用");
    expect(v.livenessLabel).toContain("240s");
  });

  it("从未成功往返过 ⇒ 文案不得声称「曾经可用」", () => {
    const v = describeBigQmtProbe({
      ...healthy, available: false, agent_unresponsive: true, liveness_failures: 2,
      last_agent_ok_age_s: null });
    expect(v.livenessTone).toBe("warn");
    expect(v.livenessLabel).not.toContain("曾经可用");
  });

  it("★ agent 元数据看着正常但不可用时，不得判 ok（假绿灯就是这一格）", () => {
    // 注意 healthy 的 agent 段完整、bridge_dir 一致、两个泵都在跑 —— 一切都「正常」。
    const v = describeBigQmtProbe({
      ...healthy, available: false, agent_unresponsive: true, liveness_failures: 2 });
    expect(v.dirTone).toBe("ok");
    expect(v.pumpOk).toBe(true);
    expect(v.livenessTone).toBe("warn");
  });

  it("agent 未应答 ⇒ unknown（无从判断，报红会制造假红）", () => {
    const v = describeBigQmtProbe({ ...healthy, agent: {}, available: false });
    expect(v.livenessTone).toBe("unknown");
    expect(v.livenessLabel).toContain("未知");
    // unknown 不进提示行（提示行只承接**确定有问题**的结论）
    expect(bigQmtProbeHint(v)).toBe("");
  });

  it("有元数据但尚无一次成功往返 ⇒ unknown，不报红", () => {
    const v = describeBigQmtProbe({
      ...healthy, available: false, agent_unresponsive: false, last_agent_ok_age_s: null });
    expect(v.livenessTone).toBe("unknown");
    expect(v.livenessLabel).toContain("尚无");
  });

  it("★ 存活面报 warn 时提示行**不得**为空（否则用户以为一切正常）", () => {
    const v = describeBigQmtProbe({
      ...healthy, root_cause: "", available: false, agent_unresponsive: true,
      liveness_failures: 3 });
    expect(v.livenessTone).toBe("warn");
    expect(bigQmtProbeHint(v)).not.toBe("");
    expect(bigQmtProbeHint(v)).toContain("不可用");
  });

  it("有 root_cause 时仍以它为准（后端单一结论不被前端存活面覆盖）", () => {
    const v = describeBigQmtProbe({
      ...healthy, root_cause: "注入函数缺失", available: false,
      agent_unresponsive: true, liveness_failures: 2 });
    expect(v.livenessTone).toBe("warn");
    expect(bigQmtProbeHint(v)).toBe("注入函数缺失");
  });

  it("lastAgentOkAgeS 原样透出（数字才透，缺省/非法为 null）", () => {
    expect(describeBigQmtProbe({ ...healthy, available: true, last_agent_ok_age_s: 12 })
      .lastAgentOkAgeS).toBe(12);
    // 注意要显式置 undefined/null 覆盖夹具里的 12（healthy 自带 last_agent_ok_age_s）。
    expect(describeBigQmtProbe({ ...healthy, available: true, last_agent_ok_age_s: undefined })
      .lastAgentOkAgeS).toBe(null);
    expect(describeBigQmtProbe({ ...healthy, available: true, last_agent_ok_age_s: null })
      .lastAgentOkAgeS).toBe(null);
  });
});
