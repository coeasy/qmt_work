import { useEffect, useState } from "react";
import { useBrokerStore } from "@/stores/broker";
import { useQuotesStore } from "@/stores/quotes";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import { isTradingHours } from "@/shared/format";
import s from "./shell.module.css";

const SOCKET_LABEL: Record<string, string> = {
  idle: "未连接",
  connecting: "连接中",
  open: "已连接",
  closed: "已断开",
};

/**
 * 底部状态栏。
 * 常驻展示「连接 / 行情通道 / 交易时段 / 时钟」——交易终端的必备信息带。
 */
export function StatusBar() {
  const connections = useBrokerStore((st) => st.connections);
  const load = useBrokerStore((st) => st.load);
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

  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    void load();
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, [load]);

  const connected = connections.filter((c) => c.connected).length;
  const active = connections.find((c) => c.active);
  const trading = isTradingHours(now);
  const subCount = Object.keys(refs).length;

  const brokerDot = connected > 0 ? s.dotOk : s.dotErr;
  const sockDot =
    socketState === "open" ? s.dotOk : socketState === "connecting" ? s.dotWarn : s.dotErr;

  return (
    <div className={s.statusbar}>
      <span className={s.statusItem}>
        <span className={[s.dot, brokerDot].join(" ")} />
        券商 {connected}/{connections.length}
        {active && <span style={{ color: "var(--text)" }}>· {active.broker_name ?? active.broker_id}</span>}
      </span>

      <span className={s.statusItem}>
        <span className={[s.dot, sockDot].join(" ")} />
        行情通道 {SOCKET_LABEL[socketState] ?? socketState}
        {subCount > 0 && <span>· 订阅 {subCount}</span>}
      </span>

      <span className={s.statusItem}>
        <span className={[s.dot, trading ? s.dotOk : s.dotIdle].join(" ")} />
        {trading ? "交易时段" : "非交易时段"}
      </span>

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
