import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmButton,
  ConfirmModal,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
} from "@/design/primitives";
import { brokerApi } from "@/services/api";
import type { AutoDetectCandidate, BigQmtProbe, BrokerDiagnostics } from "@/services/api";
import type { ConnectorInfo } from "@/services/api/broker";
import { useBrokerStore } from "@/stores/broker";
import { bigQmtProbeHint, describeBigQmtProbe } from "./bigqmtProbe";
import s from "./brokers.module.css";

/**
 * 推荐项 = 「在线客户端」：进程正在运行 **且** 已读到资金账号。
 * 满足这条就无需用户填任何东西，可一键接入。
 */
function isRecommended(c: AutoDetectCandidate): boolean {
  return c.running && !!c.default_account_id;
}

/**
 * 客户端模式的中文名。**模式键与语义必须与后端 `xtp/env.py::_MODE_LABEL` 一致**。
 *
 * 「一致」的口径是**键相同、概念相同**（full=完整版大 QMT / mini=极速版小 QMT /
 * quote=仅行情），措辞可按界面需要精简（后端那条是日志/报错文案，更长）；
 * 但**同一个模式在界面、日志、后端提示里不能出现互相矛盾的叫法** ——
 * 否则排障时对不上号（例如界面说「极速版」、日志说「小客户端」是同一物，
 * 而「独立行情」绝不能和「极速版」混为一谈，那是 miniquote 子进程）。
 *
 * 导出以便 `tests/brokerClientMode.test.ts` 静态校验（防后续改动把两种叫法改散）。
 */
export const MODE_LABEL: Record<string, string> = {
  full: "完整版（大 QMT）",
  mini: "极速版（小 QMT）",
  auto: "自动识别",
  quote: "仅行情",
};

/**
 * 模式建议的**依据**标签。
 *
 * 存在意义：「大 QMT 用大模式、小 QMT 用小模式」这条要求是否被满足，
 * 靠的就是模式是否**按实际运行进程**判定。若界面不显示依据，用户无法区分
 * 「实测出来的模式」与「按目录猜的模式」—— 两者外观一模一样，无法验收。
 */
export function modeSourceLabel(src?: string): { text: string; tone: "success" | "neutral" } {
  return src === "process"
    ? { text: "按运行进程判定", tone: "success" }
    : { text: "按目录布局推断", tone: "neutral" };
}

/**
 * 接入模式 → 后端 ``connector_key``。``direct`` 提交空串（走客户端路径直连），
 * 但它的**可用性**要看注册表里的 ``qmt.big.direct``。
 *
 * ★ 2026-10-23 后 ``qmt.big.closed`` 是唯一「可用且推荐」的大 QMT 组合。
 *   之前这里没有 closed，模板却提示「请改用闭环」—— 指引指向一条 UI 上根本
 *   选不到的路；默认值 ``direct`` 又是已停用项，用户不改就会提交必失败的连接。
 */
export const ACCESS_MODE_KEYS: Record<string, string> = {
  direct: "",
  bridgeFile: "qmt.big.bridge.file",
  bridgeRedis: "qmt.big.bridge.redis",
  bridgeZmq: "qmt.big.bridge.zmq",
  closed: "qmt.big.closed",
};

const ACCESS_MODE_LABELS: Record<string, string> = {
  direct: "xtquant 直连（极速版/完整版大客户端）",
  bridgeFile: "大 QMT 桥接 · 文件",
  bridgeRedis: "大 QMT 桥接 · Redis",
  bridgeZmq: "大 QMT 桥接 · ZMQ",
  closed: "大 QMT 闭环（盘前计划 + 日志回传）",
};

/** 该模式在连接器注册表里的 key（用于查 available / retired / recommended）。 */
export function availabilityKeyForMode(mode: string): string {
  return mode === "direct" ? "qmt.big.direct" : (ACCESS_MODE_KEYS[mode] ?? "");
}

/** 是否要填「桥接参数 / 令牌」。★ 闭环与直连都**不需要** ``bridge_dir``。 */
export function needsBridgeParams(mode: string): boolean {
  return mode === "bridgeFile" || mode === "bridgeRedis" || mode === "bridgeZmq";
}

/**
 * 该模式有没有**实时会话**。闭环是嵌入式：策略在 QMT 进程内自主执行，
 * 后端既不能握手（``autoconnect`` 必须为 false）也不能实时探测。
 *
 * ★ 少了这一条，闭环模式会被当成普通连接去建连，必然拿到 ``connected=false``，
 *   界面把一条正常登记的闭环连接报成「连接失败」—— 与假绿灯对称的假红灯。
 */
export function hasRealtimeSession(mode: string): boolean {
  return availabilityKeyForMode(mode) !== "qmt.big.closed";
}

export interface AccessModeOption {
  value: string;
  label: string;
  disabled: boolean;
}

/**
 * 由后端清单生成下拉项，而不是前端硬编码。
 * 排序：可用且推荐 → 可用 → 已停用（禁用并标明）。
 * 清单加载失败时 ``items=[]`` ⇒ 全部按「未知」处理、不禁用任何项 —— 一次网络
 * 抖动不能把用户锁死在页面外；提交时同样只在**明确知道停用**时才拦截。
 */
export function buildAccessModeOptions(items: ConnectorInfo[]): AccessModeOption[] {
  const byKey = new Map(items.map((i) => [i.key, i]));
  const rank = (mode: string): number => {
    const meta = byKey.get(availabilityKeyForMode(mode));
    if (!meta) return 1;
    if (!meta.available) return 3;
    return meta.recommended ? 0 : 1;
  };
  return Object.keys(ACCESS_MODE_KEYS)
    .sort((a, b) => rank(a) - rank(b))
    .map((mode) => {
      const meta = byKey.get(availabilityKeyForMode(mode));
      const retired = meta?.available === false;
      const recommended = !!meta?.recommended && !retired;
      const suffix = retired ? "（已停用）" : recommended ? "（推荐）" : "";
      return {
        value: mode,
        label: (ACCESS_MODE_LABELS[mode] ?? mode) + suffix,
        disabled: retired,
      };
    });
}

/** 默认接入模式：先「可用且推荐」，再「可用」；全不可用 ⇒ ``""``（界面给空态）。 */
export function defaultAccessMode(items: ConnectorInfo[]): string {
  const byKey = new Map(items.map((i) => [i.key, i]));
  const usable = Object.keys(ACCESS_MODE_KEYS).filter((mode) => {
    const meta = byKey.get(availabilityKeyForMode(mode));
    return !!meta && meta.available;
  });
  const recommended = usable.find((mode) =>
    byKey.get(availabilityKeyForMode(mode))?.recommended);
  return recommended ?? usable[0] ?? "";
}

/**
 * 快速模板：让新用户在「接入模式」下拉里不用自己猜「我这种情况该选哪个」。
 * 点模板按钮只填 accessMode（券商 id 由独立下拉控制，模板不越权）。
 *
 * 两条职责：
 *   ① 闭环模板 = 2026-10-23 后**唯一可下单**的路径，必须排第一且可套用；
 *   ② 三个历史模板 = 明确告知不可用（点了只显示原因，不写入表单）。
 *
 * 导出以便 `tests/brokerTemplates.test.ts` 静态校验，防止后续改动把它们悄悄
 * 改回「可用」或把闭环入口删掉 —— 那会让界面重新出现「推荐文件桥」式假象。
 */
export const QUICK_TEMPLATES = [
  {
    id: "qmt-big-closed",
    label: "大 QMT 闭环（推荐）",
    accessMode: "closed" as const,
    retired: false,
    hint: "盘前把计划烘进策略源码、策略在 QMT 内自主执行，结果从客户端日志回传；"
      + "本页只登记接入方式，部署与执行状态请到「QMT Agent 部署」页查看。",
  },
  {
    id: "qmt-big-bridge",
    label: "大 QMT 文件桥（已停用）",
    accessMode: "bridgeFile" as const,
    retired: true,
    hint: "已停用：策略进程禁文件读写（PermissionError: Forbidden FileIO），"
      + "该方式在当前券商环境下必然失败；请改用闭环（qmt.big.closed）部署。",
  },
  {
    id: "qmt-mini-direct",
    label: "小 QMT 极速版（已停用）",
    accessMode: "direct" as const,
    retired: true,
    hint: "已停用：miniQMT/xtquant 外接已于 2026-10-23 被券商关停；"
      + "仍有旧客户端的环境可只读查询，但无法下单。",
  },
  {
    id: "qmt-big-direct",
    label: "大 QMT 直连（已停用）",
    accessMode: "direct" as const,
    retired: true,
    hint: "已停用：xtquant 直连受授权串限制，且 miniQMT 已关停。"
      + "仅用于历史环境排查，不要用于新部署。",
  },
] as const;

/**
 * 大 QMT 桥的探针面板。
 *
 * 为什么必须渲染而不是把整块 JSON 丢出来：大小 QMT 的差异**不在「能不能连」，
 * 而在「哪一层断了」**——agent 没运行 / bridge_dir 两端不一致 / 注入函数缺失 /
 * trading_enabled 关闭 / 行情泵没跑 / 事件泵没跑，这六种情况的界面表现都是
 * 「连接成功但没有数据」，用户完全无法区分。
 *
 * 判定逻辑全部在 `bigqmtProbe.ts`（纯函数，可直接单测）；此处只负责展示。
 */
function BigQmtProbeBlock({ probe }: { probe: BigQmtProbe }) {
  if (probe.error) {
    return <div className={s.err}>大 QMT 探针读取失败：{probe.error}</div>;
  }
  const v = describeBigQmtProbe(probe);
  const hint = bigQmtProbeHint(v);

  return (
    <div className={s.diagConnSub}>
      <div>
        大 QMT 桥：传输 <b>{v.transport}</b> · 事件语义 <b>{v.eventSemantics}</b>
        {v.latencyLabel}
      </div>
      {hint ? <div className={s.err}>根因：{hint}</div> : null}
      {/* 存活面：「现在可用吗」—— 必须排在「连接过没有」之前。文件桥没有连接，
          旧实现里 agent 死掉后标志仍为真，界面会一直显示「已连接」（假绿灯）。 */}
      <div className={v.livenessTone === "warn" ? s.err : undefined}>
        可用性：{v.livenessLabel}
      </div>
      <div>
        {v.headline} · {v.dirLabel} · 回调 {v.callbackBound ? "已绑定" : "未绑定"}
        {v.directionUnknown > 0
          ? ` · 方向未知 ${v.directionUnknown} 次（勿据此判定买卖）`
          : ""}
        {v.clockOffsetMs ? ` · 时钟偏移 ${v.clockOffsetMs}ms` : ""}
      </div>
      <div>
        能力面：{v.capabilityLine}
        {v.opsDrift ? <b>（契约漂移）</b> : null}
      </div>
      {/* 两个「泵」是独立失败点：行情泵没跑 → 界面永远空白；
          事件泵没跑 → 成交回报永远到不了前端。 */}
      <div>
        事件泵 <b>{v.pumpLabel}</b>
        {v.localBridgeDir ? ` · 本机桥目录 ${v.localBridgeDir}` : ""}
        {v.dirTone === "warn" && v.agentBridgeDir
          ? ` · agent 桥目录 ${v.agentBridgeDir}`
          : ""}
      </div>
      {/* 行情订阅对账面：第 ⑦ 类故障（agent 不知道要转发什么）——
          价格永远停在种子值，而连接/健康/日志全是绿的。 */}
      <div className={v.quoteTone === "warn" ? s.err : undefined}>
        行情订阅：{v.quoteLabel}
      </div>
    </div>
  );
}

/**
 * 无效连接 = 后端判定 client_path **不存在**。
 *
 * 这类条目（历史版本 / 自动化测试残留，如 `C:/no_such_qmt/userdata_mini`）永远连不上，
 * 却安静地占满列表，让用户以为「配了很多连接却都不可用」。必须在界面上显式标记，
 * 并提供「一键选中」以便批量清理。
 *
 * ★ 只在后端明确返回 `path_exists === false` 时判定为无效；
 * undefined（旧后端未返回该字段）视为未知，**不**标记 —— 宁可漏标也不能误标。
 */
function isInvalidPath(c: { path_exists?: boolean }): boolean {
  return c.path_exists === false;
}

/**
 * 券商连接管理。
 *
 * 注意后端约束：活跃连接全局唯一（manager.py:340-348），
 * 因此「设为活跃」是单选语义，UI 需明确表达。
 */
export function Brokers() {
  const profiles = useBrokerStore((st) => st.profiles);
  const connections = useBrokerStore((st) => st.connections);
  const loading = useBrokerStore((st) => st.loading);
  const error = useBrokerStore((st) => st.error);
  const load = useBrokerStore((st) => st.load);
  const loadProfiles = useBrokerStore((st) => st.loadProfiles);
  const connect = useBrokerStore((st) => st.connect);
  const disconnect = useBrokerStore((st) => st.disconnect);
  const setActive = useBrokerStore((st) => st.setActive);
  const remove = useBrokerStore((st) => st.remove);
  const batchRemove = useBrokerStore((st) => st.batchRemove);
  const health = useBrokerStore((st) => st.health);

  // 自动识别：本机 QMT / MiniQMT 客户端探测
  const candidates = useBrokerStore((st) => st.candidates);
  const detecting = useBrokerStore((st) => st.detecting);
  const detected = useBrokerStore((st) => st.detected);
  const detectError = useBrokerStore((st) => st.detectError);
  const detect = useBrokerStore((st) => st.detect);
  const connectCandidate = useBrokerStore((st) => st.connectCandidate);

  const [brokerId, setBrokerId] = useState("");
  const [clientPath, setClientPath] = useState("");
  const [accountId, setAccountId] = useState("");
  const [accountType, setAccountType] = useState("STOCK");
  /**
   * 接入模式。初值**刻意留空**：真正的默认值由后端连接器清单决定
   * （见 ``defaultAccessMode``）。写死 "direct" 会让页面一打开就选中一个
   * 已停用的组合，用户不主动改就提交必失败的连接。
   */
  const [accessMode, setAccessMode] = useState("");
  /**
   * 后端连接器注册表（唯一真源）。加载失败时保持空表 —— 此时按「未知」处理，
   * 不禁用任何选项、提交也不拦截，避免一次网络抖动把用户锁死在页面外。
   */
  const [connectorItems, setConnectorItems] = useState<ConnectorInfo[]>([]);
  useEffect(() => {
    let alive = true;
    brokerApi.connectors()
      .then((r) => {
        if (!alive) return;
        const items = r.items ?? [];
        setConnectorItems(items);
        // 用户已手动选过就不覆盖；否则落到「可用且推荐」的模式。
        setAccessMode((prev) => prev === "" ? defaultAccessMode(items) : prev);
      })
      .catch(() => { /* 契约缺失时保持旧行为，不阻断页面 */ });
    return () => { alive = false; };
  }, []);
  const modeOptions = useMemo(
    () => buildAccessModeOptions(connectorItems), [connectorItems]);
  const selectedConnector = connectorItems.find(
    (i) => i.key === availabilityKeyForMode(accessMode));
  const [bridgeDir, setBridgeDir] = useState("");
  const [bridgeToken, setBridgeToken] = useState("");
  const isBridge = needsBridgeParams(accessMode);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  /**
   * 连接失败的多行诊断（危险色、保留换行、单独通道）。
   *
   * 不复用 `msg` 的原因：`msg` 同时承载「已取消编辑」「已批量删除 N 个连接」这类
   * 中性提示（信息色）。把 2KB 级失败诊断混进去，既分不清语义，也会让普通提示
   * 被误导成「出错了」。
   */
  const [connErr, setConnErr] = useState("");
  /**
   * 正在连接的 conn_id。
   *
   * 后端一次 connect 最坏约 60s（桥接子进程冷启动 + 候选目录×session 逐个 connect）。
   * 旧实现按钮既无 loading 也不禁用 ⇒ 用户点完毫无反馈，会反复点击，
   * 每次都在后台再起一轮 45s 的连接重试。
   */
  const [connectingId, setConnectingId] = useState("");
  /** 探测/一键连接的结果消息，显示在「检测到的本地客户端」面板内（与表单消息分开） */
  const [detectMsg, setDetectMsg] = useState("");
  /**
   * 正在启动的候选：`<root>|<mode>`；空 = 空闲。
   *
   * GUI 启动本身立即返回，但后端在返回前会跑一次进程/端口探测（约数秒），
   * 且同一 exe 连点会真的拉起多个实例 ⇒ 必须禁用并发点击。
   */
  const [launchingKey, setLaunchingKey] = useState("");

  /** 批量选择（按 conn_id）。活跃连接不可删，故不计入可选集合。 */
  const [selected, setSelected] = useState<string[]>([]);
  /** 批量删除是破坏性动作 ⇒ 走模态确认，不用 window.confirm */
  const [confirmBatch, setConfirmBatch] = useState(false);
  const [healthBusy, setHealthBusy] = useState("");
  /**
   * 非空 = 正在编辑该连接。
   *
   * 此前这一页**只能删了重建**：想改一个资金账号就得先删连接，而删掉活跃连接
   * 又被后端约束挡着 ⇒ 「改配置」这个最普通的需求无解。
   * 后端 `POST /brokers` 早支持显式 `conn_id`（= 按 id 更新、跳过同身份去重），
   * 只是界面没接上。
   */
  const [editingId, setEditingId] = useState("");
  /** conn_id → 最近一次健康探测结果 */
  const [healthMap, setHealthMap] = useState<Record<string, Record<string, unknown>>>({});

  /** 端到端诊断快照（宿主 ABI / 随包运行时 / 各连接适配器与行情泵） */
  const [diag, setDiag] = useState<BrokerDiagnostics | null>(null);
  const [diagBusy, setDiagBusy] = useState(false);
  const [diagErr, setDiagErr] = useState("");

  const runDiag = async (deep: boolean) => {
    setDiagBusy(true);
    setDiagErr("");
    try {
      setDiag(await brokerApi.diagnostics(deep));
    } catch (e) {
      setDiag(null);
      setDiagErr(e instanceof Error ? e.message : String(e));
    } finally {
      setDiagBusy(false);
    }
  };

  useEffect(() => {
    void loadProfiles();
    void load();
    // 自动识别：进页面即探测本机客户端，不需要用户先点「探测环境」
    void detect();
  }, [loadProfiles, load, detect]);

  /** 探测结果排序：推荐项（运行中 + 有账号）置顶 */
  const ordered = useMemo(
    () => [...candidates].sort((a, b) => Number(isRecommended(b)) - Number(isRecommended(a))),
    [candidates],
  );

  /**
   * 启动本机客户端（按模式）。
   *
   * 「启动大 QMT 就用大模式 / 启动小 QMT 就用小模式」这条链路的**前半段**：
   * 由用户显式选择启动哪个模式，后端拉起对应 exe；后半段（连接时用哪个数据目录）
   * 由候选里按运行进程判定的 `client_mode` 决定。
   *
   * 三种返回**必须分开呈现**（后端已如实区分，界面不得合并）：
   *   - already_running → 「这个模式的进程已在跑」，不是「本次启动成功」；
   *   - launched        → 确实拉起了 GUI，用户还需在窗口里登录；
   *   - 两者皆 false    → **没启动**（未安装该模式 exe / 启动失败），原样显示原因。
   */
  const onLaunch = async (c: AutoDetectCandidate, mode: "full" | "mini" | "quote") => {
    const key = `${c.root}|${mode}`;
    setLaunchingKey(key);
    setDetectMsg("");
    try {
      const r = await brokerApi.launchClient(c.client_path || c.root, mode);
      const who = c.broker_name || c.name || c.root;
      const label = MODE_LABEL[mode] || mode;
      if (r.already_running) {
        setDetectMsg(`${who}：${label}的进程已在运行${r.hint ? `（${r.hint}）` : ""}，无需重复启动`);
      } else if (r.launched) {
        setDetectMsg(
          `${who}：已启动 ${r.exe || label}。${r.hint || "请在弹出的窗口中完成登录，再点「连接并设为活跃」。"}`,
        );
      } else {
        setDetectMsg(`${who}：未启动 —— ${r.hint || "后端未返回原因"}`);
      }
    } catch (e) {
      setDetectMsg(`启动失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLaunchingKey("");
    }
  };

  /** 一键建连：add(autoconnect) → 设为活跃 → 回读列表 */
  const onConnectCandidate = async (c: AutoDetectCandidate) => {
    setBusy(true);
    setDetectMsg("");
    try {
      const r = await connectCandidate(c);
      const who = c.broker_name || c.name || c.root;
      setDetectMsg(
        r.ok
          ? r.reused
            // 后端按「券商 + 客户端路径 + 资金账号」复用既有连接：连点同一个候选
            // 不会再堆出重复条目，这里必须如实说「复用」而不是「已接入」。
            ? `该客户端（${who}）已存在相同连接（同客户端 + 同资金账号），已直接复用，未重复添加`
            : `已接入 ${who}；该连接已持久化，之后每次启动会自动连接`
          : `接入失败：${r.reason ?? "未知原因"}`,
      );
    } finally {
      setBusy(false);
    }
  };

  /** 套用快速模板：只填 accessMode，其余字段留空由用户填。 */
  const applyTemplate = (t: (typeof QUICK_TEMPLATES)[number]) => {
    if (t.retired) {
      setMsg(t.hint);   // 已停用方式不写入表单，避免用户填完才发现不可用
      return;
    }
    setAccessMode(t.accessMode);
    setConnErr("");
    setMsg(`已套用「${t.label}」：${t.hint}`);
  };

  /** 把探测结果填进下方手动表单，供用户确认后自行提交 */
  const onFillForm = (c: AutoDetectCandidate) => {
    setBrokerId(c.broker_id || "generic");
    setClientPath(c.client_path);
    setAccountId(c.default_account_id || c.accounts?.[0]?.account_id || "");
    setAccountType(c.accounts?.[0]?.account_type || "STOCK");
    setDetectMsg("已填入下方表单，确认无误后点「添加连接」");
  };

  const onAdd = async () => {
    const connectorKey = ACCESS_MODE_KEYS[accessMode] ?? "";
    const closedLoop = !hasRealtimeSession(accessMode);
    if (!brokerId || !accountId) {
      setMsg("券商 / 资金账号均为必填");
      return;
    }
    // ★ 提交前按后端清单拦截：明确知道已停用就不发请求，直接给原因。
    //   下拉里 disabled 只是渲染层，编辑既有连接/异步清单迟到时仍可能选中退役项。
    if (selectedConnector && selectedConnector.available === false) {
      setMsg(`该接入方式已停用：${selectedConnector.retirement_notice || "见连接器清单"}；`
        + "请改用「大 QMT 闭环」");
      return;
    }
    if (!connectorKey && !clientPath && !closedLoop) {
      setMsg("直连模式必须填客户端路径（大 QMT 桥接请切换接入模式）");
      return;
    }
    // ★ 只有三种真桥需要 bridge_dir。闭环是嵌入式（embedded），既不需要桥目录
    //   也不需要客户端路径；旧代码「有 connector_key 就要 bridgeDir」会把闭环
    //   卡在一个永远填不对的必填项上。
    if (isBridge && !bridgeDir) {
      setMsg("大 QMT 桥接模式必须填桥接参数（file=桥目录 / redis=连接串 / zmq=tcp 地址）");
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      // 不传 autoconnect ⇒ 后端默认 true：建连即拉起子进程握手（add_broker）。
      // 因此这里不能再写「点击『连接』建立会话」—— 那会引导用户去点一个已经连上的
      // 连接，把「重复操作」误当成「必要步骤」。
      const created = await brokerApi.add({
        // ★ 带 conn_id 即更新既有连接；不带才是新建（后端据此决定是否去重）
        ...(editingId ? { conn_id: editingId } : {}),
        broker_id: brokerId, client_path: clientPath,
        account_id: accountId, account_type: accountType,
        // ★ 闭环模式没有实时会话：后端默认 autoconnect=true 会去拉子进程握手，
        //   必然得到 connected=false，界面就会把一条正常登记的闭环连接报成
        //   「连接失败」—— 与假绿灯对称的假红灯。这里显式关掉建连。
        ...(closedLoop ? { autoconnect: false } : {}),
        connector_key: connectorKey,
        // 只有真桥要带桥参数；闭环带上 bridge_dir 既是脏数据也会误导后端校验。
        ...(isBridge ? { bridge_dir: bridgeDir, auth_token: bridgeToken } : {}),
      });
      const wasEdit = !!editingId;
      setClientPath("");
      setAccountId("");
      setEditingId("");
      await load();
      setMsg(
        closedLoop
          ? "已登记闭环接入方式（未建立实时会话，这是闭环的正常形态）："
            + "盘前生成计划 → 部署策略 → QMT 内自主执行；运行态请到「QMT Agent 部署」页查看"
          : wasEdit
            ? "已保存连接修改；如修改了客户端路径/账号，请点「健康」复核连通性"
            : created?.reused
              ? "已存在相同连接（同客户端 + 同资金账号），已复用，未重复添加"
              : "已添加连接并尝试建立会话；结果见下方「已有连接」列表",
      );
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onTest = async () => {
    const connectorKey = ACCESS_MODE_KEYS[accessMode] ?? "";
    const closedLoop = !hasRealtimeSession(accessMode);
    // ★ 闭环没有实时通道：EmbeddedTransport 明确拒绝实时操作。发这个探测只会
    //   拿到一次必然失败的请求，用户却会当成「闭环坏了」。直接说清去哪看运行态。
    if (closedLoop) {
      setMsg("闭环模式不支持实时探测：策略只在盘中执行且不对外提供会话；"
        + "请到「QMT Agent 部署」页查看运行态（心跳 / 订单 / 执行结果）");
      return;
    }
    if (!brokerId || (!connectorKey && !clientPath)) {
      setMsg("探测需要券商 +（直连的客户端路径 或 桥接参数）");
      return;
    }
    if (isBridge && !bridgeDir) {
      setMsg("桥接探测需要桥接参数（与大 QMT 端 agent 配置一致）");
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      const res = await brokerApi.test({
        broker_id: brokerId, client_path: clientPath,
        connector_key: connectorKey,
        ...(isBridge ? { bridge_dir: bridgeDir, auth_token: bridgeToken } : {}),
      });
      setMsg(
        res.ok
          ? connectorKey
            ? `桥接探测通过（transport=${res.transport ?? "?"}，事件语义 ${res.event_semantics ?? "?"}，` +
              `延迟上界 ${res.max_latency_ms ?? 0}ms，agent ${String((res.agent as { ver?: string } | undefined)?.ver ?? "?")}）`
            : `探测通过（运行时模式：${res.runtime_mode ?? "未知"}）`
          : `探测失败：${res.reason ?? "未知原因"}${
              res.suggestions?.length ? `；建议：${res.suggestions.join(" / ")}` : ""
            }`,
      );
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const active = connections.find((c) => c.active);

  const onBatchRemove = async () => {
    if (selected.length === 0) return;
    setBusy(true);
    setMsg("");
    try {
      await batchRemove(selected);
      setSelected([]);
      setMsg(`已批量删除 ${selected.length} 个连接`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onHealth = async (connId: string) => {
    setHealthBusy(connId);
    try {
      const res = await health(connId);
      setHealthMap((prev) => ({ ...prev, [connId]: res }));
    } catch (e) {
      setHealthMap((prev) => ({ ...prev, [connId]: { error: e instanceof Error ? e.message : String(e) } }));
    } finally {
      setHealthBusy("");
    }
  };

  /**
   * 健康状态机（后端 `gateway/health.py` 的 `conn.health_status`）中文映射。
   *
   * ★ 必须映射而不能直接透出原始值：`needs_action`（券商授权/白名单类**非自愈**失败，
   *   后端已把重连退避降到 5 分钟）是给人看的最终结论，直接显示英文枚举值等于没提示。
   */
  const HEALTH_STATUS_LABEL: Record<string, { text: string; tone: "success" | "warning" | "danger" | "neutral" }> = {
    connected: { text: "已连接", tone: "success" },
    connecting: { text: "重连中…", tone: "warning" },
    disconnected: { text: "未连接", tone: "neutral" },
    error: { text: "连接错误（自动重试中）", tone: "danger" },
    needs_action: { text: "需人工处理：券商侧拒绝连接，请按上方原因联系券商", tone: "danger" },
  };

  const healthTone = (res: Record<string, unknown> | undefined): "success" | "warning" | "danger" | "neutral" => {
    if (!res) return "neutral";
    if (res.error) return "danger";
    const mapped = HEALTH_STATUS_LABEL[String(res.status)];
    if (mapped) return mapped.tone;
    const ok = res.healthy === true || res.status === "ok" || res.connected === true;
    const bad = res.healthy === false || res.status === "fail" || res.connected === false;
    if (ok) return "success";
    if (bad) return "danger";
    return "warning";
  };
  const healthLabel = (res: Record<string, unknown> | undefined): string => {
    if (!res) return "未探测";
    if (res.error) return `探测失败：${String(res.error)}`;
    if (typeof res.summary === "string") return res.summary;
    const mapped = HEALTH_STATUS_LABEL[String(res.status)];
    if (mapped) return mapped.text;
    if (typeof res.status === "string") return res.status;
    if (typeof res.connected === "boolean") return res.connected ? "已连接" : "未连接";
    return "已探测";
  };

  const removable = connections.filter((c) => !c.active);
  const allSelected = removable.length > 0 && removable.every((c) => selected.includes(c.conn_id));
  /** 可删且路径无效的连接 —— 供「选中无效项」一键勾选后批量清理 */
  const invalidRemovable = removable.filter(isInvalidPath);

  return (
    <div className={s.wrap}>
      <Panel
        title={detected ? `检测到的本地客户端（${candidates.length}）` : "检测到的本地客户端"}
        extra={
          <Button size="sm" variant="ghost" onClick={() => void detect()} disabled={detecting}>
            {detecting ? "探测中…" : "重新探测"}
          </Button>
        }
      >
        {detecting && <Spinner label="正在扫描本机 QMT / MiniQMT 客户端…" />}
        {!detecting && detectError && <div className={s.err}>探测失败：{detectError}</div>}
        {!detecting && !detectError && detected && candidates.length === 0 && (
          <EmptyState text="未检测到本机 QMT / MiniQMT 客户端，可在下方手动填写" />
        )}
        {!detecting &&
          ordered.map((c) => (
            <div
              key={c.root}
              className={[s.cand, isRecommended(c) ? s.candTop : ""].filter(Boolean).join(" ")}
            >
              <div className={s.candMain}>
                <div className={s.itemTitle}>
                  {c.broker_name || c.name || c.root}
                  {c.running ? (
                    <Badge tone="success">运行中{c.pid ? ` · pid ${c.pid}` : ""}</Badge>
                  ) : (
                    <Badge tone="neutral">未运行</Badge>
                  )}
                  {isRecommended(c) && <Badge tone="info">推荐</Badge>}
                  <Badge tone="neutral">{MODE_LABEL[c.client_mode] || c.client_mode || "未知模式"}</Badge>
                  <Badge tone={modeSourceLabel(c.mode_source).tone}>
                    {modeSourceLabel(c.mode_source).text}
                  </Badge>
                </div>
                <div className={s.itemSub}>{c.root}</div>
                <div className={s.itemSub}>
                  {c.default_account_id
                    ? `资金账号 ${c.default_account_id}${
                        c.accounts && c.accounts.length > 1 ? `（共 ${c.accounts.length} 个）` : ""
                      }`
                    : "未发现资金账号，需手动填写"}
                  {" · "}
                  {c.client_path}
                </div>
              </div>
              <div className={s.itemActions}>
                {/*
                  三个启动模式**各自独立**（后端按模式分别判定「是否已在运行」）：
                  大客户端在跑不会挡住极速版的启动 —— 旧后端把二者混成一个标志，
                  「启动小 QMT」会被误报「已在运行」而永远拉不起来。
                */}
                <Button
                  size="sm"
                  variant="ghost"
                  title="启动完整版大客户端 XtItClient.exe（数据目录 userdata）"
                  disabled={!!launchingKey}
                  onClick={() => void onLaunch(c, "full")}
                >
                  {launchingKey === `${c.root}|full` ? "启动中…" : "启动大 QMT"}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  title="启动极速版小客户端 XtMiniQmt.exe（数据目录 userdata_mini）"
                  disabled={!!launchingKey}
                  onClick={() => void onLaunch(c, "mini")}
                >
                  {launchingKey === `${c.root}|mini` ? "启动中…" : "启动小 QMT"}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  title="只启动独立行情 miniquote.exe，为大客户端补齐 58610 行情服务"
                  disabled={!!launchingKey}
                  onClick={() => void onLaunch(c, "quote")}
                >
                  {launchingKey === `${c.root}|quote` ? "启动中…" : "仅补行情"}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy || !c.default_account_id}
                  onClick={() => void onConnectCandidate(c)}
                >
                  连接并设为活跃
                </Button>
                <Button size="sm" variant="ghost" onClick={() => onFillForm(c)}>
                  填入表单
                </Button>
              </div>
            </div>
          ))}
        {detectMsg && <div className={s.msg}>{detectMsg}</div>}
      </Panel>

      <Panel
        title={editingId ? `编辑连接 ${editingId}` : "新增连接"}
        extra={
          editingId ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setEditingId("");
                setClientPath("");
                setAccountId("");
                setMsg("已取消编辑");
              }}
            >
              取消编辑
            </Button>
          ) : null
        }
      >
        <div className={s.form}>
          {!editingId && (
            <div
              style={{
                display: "flex",
                gap: 6,
                padding: 8,
                background: "var(--bg-3, rgba(255,255,255,0.02))",
                border: "1px dashed var(--border)",
                borderRadius: "var(--radius)",
                flexWrap: "wrap",
                alignItems: "center",
                fontSize: "var(--font-sm)",
              }}
            >
              <span style={s.itemSub ? { opacity: 0.7 } : {}}>快速模板：</span>
              {QUICK_TEMPLATES.map((t) => (
                <Button
                  key={t.id}
                  size="sm"
                  variant="ghost"
                  title={t.hint}
                  onClick={() => applyTemplate(t)}
                >
                  {t.label}
                </Button>
              ))}
            </div>
          )}
          <FormRow label="券商">
            <Select
              value={brokerId}
              onChange={(e) => setBrokerId(e.target.value)}
              placeholder="请选择"
              options={profiles.map((p) => ({
                value: p.id,
                label: p.planned ? `${p.name}（未支持 SDK）` : p.name,
                disabled: p.planned === true,
              }))}
            />
          </FormRow>
          <FormRow label="接入模式">
            <Select
              value={accessMode}
              onChange={(e) => setAccessMode(e.target.value)}
              placeholder="当前环境无可用接入方式（请查看连接器清单）"
              options={modeOptions}
            />
          </FormRow>
          {selectedConnector?.available === false ? (
            <div className={s.err}>
              该接入方式已停用：{selectedConnector.retirement_notice || "见连接器清单"}。
              请改用「大 QMT 闭环」。
            </div>
          ) : null}
          {isBridge ? (
            <>
              <FormRow label="桥接参数">
                <Input
                  value={bridgeDir}
                  onChange={(e) => setBridgeDir(e.target.value)}
                  placeholder="file=桥目录（须与大 QMT 端 agent_config.json 完全一致）；redis=redis://127.0.0.1:6379/0；zmq=tcp://127.0.0.1:5555"
                  mono
                />
              </FormRow>
              <FormRow label="桥接令牌">
                <Input
                  value={bridgeToken}
                  onChange={(e) => setBridgeToken(e.target.value)}
                  placeholder="与 agent 端 auth_token 一致（留空=不校验，仅调试）"
                  mono
                />
              </FormRow>
              <div className={s.itemSub}>
                桥接模式无需客户端路径：资金账号仍必填（将随请求传给大 QMT 端 agent）。
                部署步骤见 backend/agent_bigqmt/DEPLOY.md。
              </div>
            </>
          ) : !hasRealtimeSession(accessMode) ? (
            // ★ 闭环既不填 bridge_dir 也不填 client_path：策略在 QMT 进程内跑，
            //   本页只登记「接入方式」这个元数据。把桥接/直连的输入框摆在这里
            //   会让人以为不填就跑不起来，是另一种形式的假象。
            <div className={s.itemSub}>
              闭环模式无需桥接参数，也没有实时会话：盘前把计划烘进策略源码，
              QMT 内策略自主执行，结果从客户端日志回传。
              部署与运行态请到「QMT Agent 部署」页查看（生成计划 → 部署策略 → 查看执行）。
            </div>
          ) : (
            <FormRow label="客户端路径">
              <Input
                value={clientPath}
                onChange={(e) => setClientPath(e.target.value)}
                placeholder="如 C:/qmt/userdata_mini（极速版）或 userdata（完整版）"
                mono
              />
            </FormRow>
          )}
          <FormRow label="资金账号">
            <Input value={accountId} onChange={(e) => setAccountId(e.target.value)} mono />
          </FormRow>
          <FormRow label="账户类型">
            <Select
              value={accountType}
              onChange={(e) => setAccountType(e.target.value)}
              options={[
                { value: "STOCK", label: "股票" },
                { value: "CREDIT", label: "信用" },
                { value: "OPTION", label: "期权" },
                { value: "FUTURES", label: "期货" },
              ]}
            />
          </FormRow>
          <div className={s.actions}>
            <Button onClick={onTest} disabled={busy}>
              探测环境
            </Button>
            <Button variant="primary" onClick={onAdd} disabled={busy}>
              {editingId ? "保存修改" : "添加连接"}
            </Button>
          </div>
          {msg && <div className={s.msg}>{msg}</div>}
          {/* 连接失败：多行长诊断必须保留换行（CSS pre-wrap）并用危险色，
              否则「★ 客户端侧根因 / 日志证据 / 官方四步排查」会塌成一坨看不见结构 */}
          {connErr && <div className={s.msgErr}>{connErr}</div>}
        </div>
      </Panel>

      <Panel
        title={`已有连接（${connections.length}）`}
        extra={
          <Button size="sm" variant="ghost" onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        <div className={s.batchBar}>
          <label className={s.checkCell}>
            <input
              type="checkbox"
              checked={allSelected}
              disabled={removable.length === 0}
              onChange={(e) =>
                setSelected(e.target.checked ? removable.map((c) => c.conn_id) : [])
              }
              aria-label="全选可删除连接"
            />
            全选可删
          </label>
          <span className={s.selCount}>已选 {selected.length} 项</span>
          {/* 无效连接（路径不存在）永远连不上，一键勾选后走批量删除清理 */}
          {invalidRemovable.length > 0 && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => setSelected(invalidRemovable.map((c) => c.conn_id))}
              title="勾选所有「客户端路径不存在」的连接（永远连不上的历史/测试残留）"
            >
              选中无效项（{invalidRemovable.length}）
            </Button>
          )}
          {/* 批量删除是破坏性动作 ⇒ 模态确认（行内逐条删除才用两段式） */}
          <Button
            size="sm"
            variant="danger"
            disabled={busy || selected.length === 0}
            onClick={() => setConfirmBatch(true)}
          >
            批量删除
          </Button>
        </div>
        <div className={s.list}>
          {loading && <Spinner label="加载中…" />}
          {!loading && error && <div className={s.err}>{error}</div>}
          {!loading && connections.length === 0 && (
            <EmptyState text="尚未添加任何券商连接" />
          )}
          {connections.map((c) => (
            <div key={c.conn_id} className={s.item}>
              <label className={s.checkCell}>
                <input
                  type="checkbox"
                  checked={selected.includes(c.conn_id)}
                  disabled={c.active === true}
                  onChange={(e) =>
                    setSelected((prev) =>
                      e.target.checked
                        ? [...prev, c.conn_id]
                        : prev.filter((x) => x !== c.conn_id),
                    )
                  }
                  aria-label={`选择连接 ${c.broker_name ?? c.broker_id}`}
                />
              </label>
              <div className={s.itemMain}>
                <div className={s.itemTitle}>
                  {c.broker_name ?? c.broker_id}
                  {c.active && <Badge tone="info">活跃</Badge>}
                  <Badge tone={c.connected ? "success" : "danger"}>
                    {c.connected ? "已连接" : "未连接"}
                  </Badge>
                  {/* 路径不存在的残留连接：永远连不上，必须让用户一眼看出来 */}
                  {isInvalidPath(c) && <Badge tone="danger">路径无效</Badge>}
                  {c.runtime_mode && <Badge tone="neutral">{c.runtime_mode}</Badge>}
                </div>
                <div className={s.itemSub}>
                  {c.account_id ?? "—"} · {c.account_type ?? "—"} · {c.client_path ?? "—"}
                </div>
                {/* 最近一次失败原因：后端已压成单行（`brief_error`），此处单行省略 +
                    title 悬停看全文。此前该字段前端**完全没消费** ⇒ 用户在列表里
                    看不到任何失败原因，只能去翻日志。 */}
                {!c.connected && c.last_error && (
                  <div className={s.itemErr} title={c.last_error}>
                    {c.last_error}
                  </div>
                )}
                {healthMap[c.conn_id] && (
                  <div className={s.healthRow}>
                    <Badge tone={healthTone(healthMap[c.conn_id])}>
                      {healthLabel(healthMap[c.conn_id])}
                    </Badge>
                  </div>
                )}
              </div>
              <div className={s.itemActions}>
                {c.connected ? (
                  <Button size="sm" onClick={() => void disconnect(c.conn_id)}>
                    断开
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={connectingId !== ""}
                    title="连接最坏需约 60s（要逐个尝试候选数据目录与 session），期间请勿重复点击"
                    onClick={() => {
                      setConnErr("");
                      setConnectingId(c.conn_id);
                      void connect(c.conn_id)
                        .then((r) => {
                          if (!r.ok) setConnErr(`连接失败：${r.reason ?? "未知原因"}`);
                        })
                        .finally(() => setConnectingId(""));
                    }}
                  >
                    {connectingId === c.conn_id ? "连接中…" : "连接"}
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={c.active === true}
                  onClick={() => void setActive(c.conn_id)}
                >
                  设为活跃
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setEditingId(c.conn_id);
                    setBrokerId(c.broker_id || "generic");
                    setClientPath(c.client_path || "");
                    setAccountId(c.account_id || "");
                    setAccountType(c.account_type || "STOCK");
                    setMsg(`正在编辑连接 ${c.conn_id}，改完点下方「保存修改」`);
                  }}
                >
                  编辑
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={healthBusy === c.conn_id}
                  onClick={() => void onHealth(c.conn_id)}
                >
                  {healthBusy === c.conn_id ? "探测中…" : "健康"}
                </Button>
                {/* 列表行内删除 ⇒ 两段式确认。删除**活跃连接**已由 disabled 挡住
                    （后端约束：活跃连接全局唯一），不必再在文案里重复解释。 */}
                <ConfirmButton
                  variant="danger"
                  disabled={c.active === true}
                  confirmText="确认删除"
                  title={`删除连接 ${c.broker_name ?? c.broker_id}`}
                  onConfirm={() => void remove(c.conn_id)}
                >
                  删除
                </ConfirmButton>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel
        title="连接诊断"
        extra={
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="sm" variant="ghost" disabled={diagBusy} onClick={() => void runDiag(false)}>
              {diagBusy ? "诊断中…" : "运行诊断"}
            </Button>
            <Button size="sm" variant="ghost" disabled={diagBusy} onClick={() => void runDiag(true)}>
              深度诊断
            </Button>
          </div>
        }
      >
        <div className={s.diagBody}>
          {diagErr && <div className={s.err}>{diagErr}</div>}
          {!diag && !diagErr && (
            <EmptyState text="尚未运行诊断。若出现「桥接子进程握手失败」，先跑一次诊断看宿主 ABI 与随包桥接运行时是否匹配 —— 不必去翻被截断的 stderr。" />
          )}
          {diag && (
            <>
              <div className={s.diagRow}>
                <span>宿主 Python</span>
                <span className={s.mono}>{diag.host_python ?? "—"}</span>
              </div>
              <div className={s.diagRow}>
                <span>宿主 ABI</span>
                <span className={s.mono}>{String(diag.host_abi ?? "—")}</span>
              </div>
              <div className={s.diagRow}>
                <span>随包桥接运行时</span>
                <span className={s.mono}>
                  {Object.keys(diag.bundled_runtimes ?? {}).length === 0
                    ? "无（桥接子进程将回退系统解释器）"
                    : Object.entries(diag.bundled_runtimes ?? {})
                        .map(([abi, p]) => `ABI ${abi} → ${p}`)
                        .join("；")}
                </span>
              </div>
              {diag.system_runtimes && (
                <div className={s.diagRow}>
                  <span>系统运行时</span>
                  <span className={s.mono}>
                    {Object.entries(diag.system_runtimes)
                      .map(([abi, p]) => `ABI ${abi} → ${p}`)
                      .join("；") || "未发现"}
                  </span>
                </div>
              )}

              {(diag.connections ?? []).length === 0 ? (
                <EmptyState text="无已添加的连接（诊断快照本身正常）" />
              ) : (
                (diag.connections ?? []).map((c) => (
                  <div key={c.conn_id} className={s.diagConn}>
                    <div className={s.diagConnTitle}>
                      <span>{c.broker_name ?? c.broker_id ?? c.conn_id}</span>
                      <Badge tone={c.connected ? "success" : "danger"}>
                        {c.connected ? "已连接" : "未连接"}
                      </Badge>
                      {/* 「握手成功」与「行情在推」是两件事：泵没跑时界面会一直空 */}
                      <Badge tone={c.pump_running ? "success" : "warning"}>
                        行情泵 {c.pump_running ? "运行中" : "未运行"}
                      </Badge>
                      {c.active && <Badge tone="info">活跃</Badge>}
                    </div>
                    <div className={s.diagConnSub}>
                      适配器 {c.adapter ?? "—"} · 健康 {c.health_status ?? "—"} · 重连{" "}
                      {c.reconnect_attempts ?? 0} 次 · {c.client_path ?? "—"}
                    </div>
                    {c.last_error && <div className={s.err}>最近错误：{c.last_error}</div>}
                    {c.runtime_plan && (
                      <div className={s.diagConnSub}>桥接方案：{JSON.stringify(c.runtime_plan)}</div>
                    )}
                    {c.bigqmt && <BigQmtProbeBlock probe={c.bigqmt} />}
                  </div>
                ))
              )}
            </>
          )}
        </div>
      </Panel>

      <div className={s.note}>
        当前活跃连接：<strong>{active?.broker_name ?? active?.broker_id ?? "无"}</strong>
        。后端活跃连接全局唯一，多账户并行下单需经「多账户网格」批量接口。
      </div>

      <ConfirmModal
        open={confirmBatch}
        danger
        title="批量删除连接"
        confirmText={`确认删除 ${selected.length} 个`}
        message={
          <>
            将移除 <b>{selected.length}</b> 个券商连接。活跃连接不会被选中，但其余连接将全部移除，
            且<b>不可恢复</b>（需重新添加并重新连接）。
          </>
        }
        onCancel={() => setConfirmBatch(false)}
        onConfirm={() => {
          setConfirmBatch(false);
          void onBatchRemove();
        }}
      />
    </div>
  );
}

export default Brokers;
