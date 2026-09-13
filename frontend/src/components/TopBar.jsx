// TopBar（v2 统一顶栏，44px）：
//   左区：Logo + 全局搜索 + 4 个下拉菜单（行情/研究/交易/系统）
//   右区：指数跑马灯 + 券商状态灯 + WS延迟 + 时钟
//
// 合并原 MenuBar(32px) + BrokerBar(28px) = 60px → 44px，节省一行。
// 原 BottomDock 和 QuickBar 彻底移除，5 个 Tab 提升为独立页面。
import { useState, useRef, useEffect } from "react";
import Ticker from "./Ticker.jsx";
import GlobalSearch from "./GlobalSearch.jsx";
import { PAGES } from "../pagesRegistry.jsx";
import { t } from "../lib/i18n.js";
import { emit } from "../lib/eventBus";
import { useBroker } from "../BrokerContext.jsx";
import { useQuoteHub } from "../lib/quoteHub.jsx";
import { useSystemStatus } from "../hooks/useSystemWS.js";

const MENUS = [
  { label: "行情", keys: ["quoteboard", "quote", "mktstructure", "sector_radar", "moneyflow", "deal_feed"] },
  { label: "研究", keys: ["screen", "factor_hub"] },
  { label: "交易", keys: ["trade", "algo", "watchlist"] },
  { label: "系统", keys: ["dashboard", "brokers", "accounts", "sysstatus", "system_log", "settings"] },
];

export default function TopBar() {
  const [openIdx, setOpenIdx] = useState(-1);
  const closeTimer = useRef(null);
  const [clock, setClock] = useState("");

  // 时钟
  useEffect(() => {
    const fmt = () => setClock(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    fmt();
    const id = setInterval(fmt, 1000);
    return () => clearInterval(id);
  }, []);

  const scheduleClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setOpenIdx(-1), 180);
  };
  const cancelClose = () => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; } };
  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);

  const open = (key) => { emit("nav", key); setOpenIdx(-1); };
  const nav = (k) => emit("nav", k);

  // Broker + WS 状态
  const { brokers, activeId, activeBroker, connectedCount, setActive } = useBroker();
  const hub = useQuoteHub();
  const { latency } = useSystemStatus();
  const wsOk = hub.state === "connected";
  const brokerOk = !!activeBroker?.connected;
  const latencyStr = latency != null ? `${latency}ms` : "--";

  return (
    <div className="topbar" style={{ height: 44 }}>
      <div className="topbar-left">
        <span className="topbar-brand" title="qmt_work 量化平台" onClick={() => nav("dashboard")} style={{ cursor: "pointer" }}>qmt_work</span>
        <span className="topbar-sep" aria-hidden="true" />
        <GlobalSearch />
        <span className="topbar-sep" aria-hidden="true" />
        {MENUS.map((m, i) => (
          <div
            className={`menu ${openIdx === i ? "open" : ""}`}
            key={m.label}
            onMouseEnter={() => { cancelClose(); setOpenIdx(i); }}
            onMouseLeave={scheduleClose}
          >
            <button
              className="menu-btn"
              onClick={() => setOpenIdx((v) => (v === i ? -1 : i))}
              aria-expanded={openIdx === i}
            >{t(`menu.${m.label}`)}</button>
            {openIdx === i && (
              <div className="menu-dropdown" onMouseEnter={cancelClose} onMouseLeave={scheduleClose}>
                {m.keys.map((k) => {
                  const item = PAGES[k];
                  if (!item) return null;
                  return (
                    <div key={k} className="menu-item" onClick={() => open(k)}>
                      <span className="menu-item-label">{t(`page.${k}`)}</span>
                      {item.hint && <span className="menu-item-hint">{item.hint}</span>}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="topbar-right" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <Ticker />
        <span className="topbar-sep" aria-hidden="true" />
        {/* 券商状态灯 */}
        <span className={`status-dot ${brokerOk ? "ok" : "fail"}`} title={brokerOk ? "已连接" : "未连接"} aria-hidden="true" />
        <span style={{ fontSize: 12, color: brokerOk ? "var(--up)" : "var(--danger)" }}>
          {brokers.length === 0 ? "无券商" : brokerOk ? "已连接" : "未连接"}
        </span>
        {brokers.length > 1 && (
          <select
            style={{ fontSize: 11, padding: "1px 4px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 3, color: "var(--text)", maxWidth: 120 }}
            value={activeId}
            onChange={(e) => setActive(e.target.value)}
            title="切换券商"
          >
            {brokers.map((b) => (
              <option key={b.conn_id} value={b.conn_id}>{b.broker_name}</option>
            ))}
          </select>
        )}
        {/* WS 状态 */}
        <span className={`status-dot ${wsOk ? "ok" : "fail"}`} title={wsOk ? "行情实时" : "行情离线"} aria-hidden="true" />
        <span style={{ fontSize: 11, color: "var(--text-dim)" }}>📡{latencyStr}</span>
        {/* 时钟 */}
        <span style={{ fontSize: 12, color: "var(--text-dim)", fontFamily: "monospace" }}>{clock}</span>
      </div>
    </div>
  );
}
