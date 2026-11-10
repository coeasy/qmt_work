import { Badge, Panel } from "@/design/primitives";
import s from "./closedLoopNotice.module.css";

/**
 * 新规下的下单形态说明（2026-10-23 起）。
 *
 * ★ 这个组件存在的唯一理由：**不给即时反馈的假象**。
 *
 * 2026-08-28 中国证券业协会与基金业协会联合发布《证券公司交易信息系统接入
 * 管理规范（试行）》，2026-10-23 收盘后生效。它同时关掉三件事：
 *   ① 策略沙箱内的文件读写（`PermissionError: Forbidden FileIO`）；
 *   ② 对外网络访问 —— **包括 127.0.0.1 / localhost 回环**；
 *   ③ miniQMT / xtquant 通道。
 *
 * 前两条的区别是关键：文件违规只是「这一次调用失败」，网络违规的处置是
 * **停模（策略被强制停止）+ 防外挂弹窗**。所以「外部发指令 → 策略执行」
 * 这类实现不是「性能差一点」，而是拿账户去赌。
 *
 * 于是产品上必须把「远程」拆成**四个可交付能力**，而不是留一个假的
 * 「远程买入」按钮（方案 §7.3）：
 *   远程监控 ✅ / 远程干预 ✅ / 远程决策 ✅（分钟级）/ 远程实时下单 ❌
 *
 * ★ 本页的「买入/卖出」仍然可用 —— 它是**本地**手动下单，人在 QMT 前操作。
 *   这个组件只负责把「哪一条路还在、延迟是多少」写清楚，不改任何下单逻辑。
 */
export interface ClosedLoopNoticeProps {
  /** 紧凑模式：只留一行结论 + 展开提示（嵌入空间紧张的页面时用） */
  compact?: boolean;
}

interface Capability {
  key: string;
  label: string;
  ok: boolean;
  latency: string;
  note: string;
}

const CAPABILITIES: Capability[] = [
  {
    key: "monitor",
    label: "远程监控",
    ok: true,
    latency: "秒级",
    note: "策略把事件打到客户端日志，外部跟随日志读取持仓 / 成交 / 异常",
  },
  {
    key: "intervene",
    label: "远程干预",
    ok: true,
    latency: "秒级",
    note: "一键急停（策略内 guard.stop + 智能插座双保险），只停不买",
  },
  {
    key: "decide",
    label: "远程决策",
    ok: true,
    latency: "分钟级",
    note: "盘前生成/修改计划，落地由本地或远控完成 —— 延迟是诚实的分钟级",
  },
  {
    key: "realtime",
    label: "远程实时下单",
    ok: false,
    latency: "—",
    note: "程序化外接被禁；网络类违规的处置是停模，不是拒绝一次调用",
  },
];

const STAGES = [
  { no: "①", name: "生成计划", detail: "盘前选股/定权重 → plan.json（含时间窗与风控闸门）" },
  { no: "②", name: "部署策略", detail: "计划烘焙进策略源码 → 人工覆盖一次（这是一次可审计的动作）" },
  { no: "③", name: "查看执行", detail: "盘中策略自治执行；外部只看日志回传，不回传指令" },
];

export function ClosedLoopNotice({ compact = false }: ClosedLoopNoticeProps) {
  const blocked = CAPABILITIES.filter((c) => !c.ok);

  if (compact) {
    return (
      <div className={s.compact} role="note">
        <Badge tone="warning">新规</Badge>
        <span>
          远程实时下单已关闭（网络类违规的处置是<strong>停模</strong>）；
          仍可用的形态是「生成计划 → 部署策略 → 查看执行」。
        </span>
      </div>
    );
  }

  return (
    <Panel
      title="新规下的下单形态（2026-10-23 起）"
      extra={<Badge tone="warning">{blocked.length} 项不可用</Badge>}
      className={s.panel}
    >
      <p className={s.lead}>
        策略沙箱内<strong>禁文件读写</strong>，且<strong>禁一切对外网络</strong>
        （含 <code>127.0.0.1</code> 回环）。文件违规只是这一次调用失败，
        网络违规的处置是<strong>停模</strong> —— 所以「外部发指令、策略执行」
        这条路不是慢一点，是不能走。
      </p>

      <div className={s.grid}>
        {CAPABILITIES.map((cap) => (
          <div key={cap.key} className={cap.ok ? s.cap : s.capOff}>
            <div className={s.capHead}>
              <Badge tone={cap.ok ? "success" : "danger"}>{cap.ok ? "可用" : "不可用"}</Badge>
              <span className={s.capLabel}>{cap.label}</span>
              <span className={s.capLatency}>{cap.latency}</span>
            </div>
            <div className={s.capNote}>{cap.note}</div>
          </div>
        ))}
      </div>

      <div className={s.stages}>
        {STAGES.map((stage) => (
          <div key={stage.no} className={s.stage}>
            <span className={s.stageNo}>{stage.no}</span>
            <span className={s.stageName}>{stage.name}</span>
            <span className={s.stageDetail}>{stage.detail}</span>
          </div>
        ))}
      </div>

      <p className={s.footnote}>
        本页的「买入 / 卖出」仍然可用 —— 它是<strong>本地手动</strong>下单，
        需本人在 QMT 前操作并确认。它<strong>不是</strong>远程实时下单，
        也不提供「点了就成交」的假象。
      </p>
    </Panel>
  );
}
