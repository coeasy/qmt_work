import { useEffect, useState } from "react";
import { brokerBadge, useBrokerStore } from "@/stores/broker";
import { useQuotesStore } from "@/stores/quotes";
import { sessionBadge, useSessionStore } from "@/stores/session";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import s from "./shell.module.css";

const SOCKET_LABEL: Record<string, string> = {
  idle: "未连接",
  connecting: "连接中",
  open: "已连接",
  closed: "已断开",
};

/** 服务端交易时段的轮询间隔。日历/盘中状态变化很慢，30s 足够且成本可忽略。 */
const SESSION_POLL_MS = 30_000;

/**
 * 底部状态栏。
 * 常驻展示「连接 / 行情通道 / 交易时段 / 时钟」——交易终端的必备信息带。
 *
 * ★ 基础状态设计原则（2026-09-17）：**每一个非正常态都要给出路**。
 * 旧实现里「券商 0/0」既不可点击、也不说怎么办，用户只能自己猜；
 * 现在未连券商/状态未知都是**可点击的引导**（点击进「连接管理」或重试）。
 */
export function StatusBar() {
  const connections = useBrokerStore((st) => st.connections);
  const load = useBrokerStore((st) => st.load);
  const loading = useBrokerStore((st) => st.loading);
  const brokerError = useBrokerStore((st) => st.error);
  const socketState = useQuotesStore((st) => st.socketState);
  const refs = useQuotesStore((st) => st.refs);
  const theme = useUiStore((st) => st.theme);
  const themePref = useUiStore((st) => st.themePref);
  const toggleTheme = useUiStore((st) => st.toggleTheme);
  const updown = useUiStore((st) => st.updown);
  const setUpdown = useUiStore((st) => st.setUpdown);
  const panelOpen = useUiStore((st) => st.dataPanelOpen);
  const togglePanel = useUiStore((st) => st.toggleDataPanel);
  const setCommandOpen = useUiStore((st) => st.setCommandOpen);
  const tabs = useWorkspaceStore((st) => st.tabs);
  const openPage = useWorkspaceStore((st) => st.open);
  const session = useSessionStore((st) => st.session);
  const refreshSession = useSessionStore((st) => st.refresh);

  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    void load();
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, [load]);

  // 服务端权威交易时段（含节假日感知）；拿不到时 sessionBadge 自动降级本地估算
  useEffect(() => {
    void refreshSession();
    const t = window.setInterval(() => void refreshSession(), SESSION_POLL_MS);
    return () => clearInterval(t);
  }, [refreshSession]);

  const active = connections.find((c) => c.active);
  const subCount = Object.keys(refs).length;
  const sess = sessionBadge(session, now);

  // ---- 券商态：三档 + 每档都有出路（语义见 stores/broker.ts::brokerBadge）----
  const badge = brokerBadge(connections, loading, brokerError);
  const brokerDot =
    badge.tone === "ok" ? s.dotOk : badge.tone === "warn" ? s.dotWarn : badge.tone === "err" ? s.dotErr : s.dotIdle;
  const brokerAction =
    badge.action === "retry"
      ? () => void load()
      : badge.action === "manage"
        ? () => openPage("brokers", {}, { title: "连接管理" })
        : null;

  const sockDot =
    socketState === "open" ? s.dotOk : socketState === "connecting" ? s.dotWarn : s.dotErr;

  return (
    <div className={s.statusbar}>
      <button
        type="button"
        className={s.statusItemBtn}
        onClick={() => brokerAction?.()}
        disabled={!brokerAction}
        title={badge.title}
      >
        <span className={[s.dot, brokerDot].join(" ")} />
        {badge.label}
        {active && (
          <span style={{ color: "var(--text)" }}>· {active.broker_name ?? active.broker_id}</span>
        )}
      </button>

      <span className={s.statusItem}>
        <span className={[s.dot, sockDot].join(" ")} />
        行情通道 {SOCKET_LABEL[socketState] ?? socketState}
        {subCount > 0 && <span>· 订阅 {subCount}</span>}
      </span>

      <button
        type="button"
        className={s.statusItemBtn}
        onClick={() => openPage("sysstatus", {}, { title: "系统状态" })}
        title={`${sess.title}\n点击查看「系统状态」`}
      >
        <span className={[s.dot, sess.tone === "ok" ? s.dotOk : s.dotIdle].join(" ")} />
        {sess.label}
      </button>

      <span className={s.statusItem}>窗口 {tabs.length}</span>

      <span className={s.statusSpacer} />

      <button type="button" className={s.statusBtn} onClick={() => setCommandOpen(true)}>
        命令面板 ⌘K
      </button>
      <button
        type="button"
        className={s.statusBtn}
        onClick={() => setUpdown(updown === "red-up" ? "green-up" : "red-up")}
        title="切换涨跌配色"
      >
        {updown === "red-up" ? "红涨绿跌" : "绿涨红跌"}
      </button>
      <button type="button" className={s.statusBtn} onClick={togglePanel}>
        {panelOpen ? "隐藏侧栏" : "显示侧栏"}
      </button>
      <button
        type="button"
        className={s.statusBtn}
        onClick={toggleTheme}
        title={
          themePref === "auto"
            ? "当前跟随系统主题，点击切换为固定主题"
            : "点击切换明暗（覆盖「跟随系统」）"
        }
      >
        {theme === "dark" ? "浅色" : "深色"}
        {themePref === "auto" && <span style={{ color: "var(--text-faint)" }}>·自动</span>}
      </button>

      <span className={s.statusItem}>
        {now.toLocaleTimeString("zh-CN", { hour12: false })}
      </span>
    </div>
  );
}
