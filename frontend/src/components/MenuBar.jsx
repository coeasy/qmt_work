// 顶部菜单栏（通达信式）：主菜单 + 右侧指数跑马灯。
// - 菜单 dropdown 浮层（绝对定位，z-index:100），不会被下方元素遮挡
// - 键盘可达：↑↓切换、Enter 打开、Esc 关闭（useHotkeys.js 已处理菜单外键位）
// - 鼠标 hover 打开，离开 200ms 延迟关闭（避免移到 dropdown 时闪烁）
import { useState, useRef, useEffect } from "react";
import Ticker from "./Ticker.jsx";
import { PAGES } from "../pagesRegistry.jsx";

// 9 个主菜单（按 TDX 习惯顺序）：系统 / 行情 / 分析 / 交易 / 策略 / 组合 / 研究 / 信号 / 账户 / 运维
const MENUS = [
  { label: "系统", keys: ["dashboard", "settings", "sysstatus"] },
  { label: "行情", keys: ["quote", "boards", "etfs", "quoteboard", "markettools", "reference"] },
  { label: "分析", keys: ["quote", "boards", "factors"] },
  { label: "交易", keys: ["trade", "limitup", "algo", "paper"] },
  { label: "策略", keys: ["strategies", "strmarket", "target", "rebalance"] },
  { label: "研究", keys: ["backtest", "factors", "research"] },
  { label: "信号", keys: ["signal", "alerts", "notifications", "webhooks"] },
  { label: "账户", keys: ["brokers", "accounts"] },
  { label: "运维", keys: ["sysstatus", "audit", "reconcile"] },
];

export default function MenuBar() {
  const [openIdx, setOpenIdx] = useState(-1);
  const closeTimer = useRef(null);

  // 统一关闭
  const scheduleClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setOpenIdx(-1), 180);
  };
  const cancelClose = () => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; } };

  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);

  const open = (key) => { window.dispatchEvent(new CustomEvent("nav", { detail: key })); setOpenIdx(-1); };

  return (
    <div className="topbar">
      <div className="topbar-left">
        <span className="topbar-brand" title="qmt_work 量化平台">qmt_work</span>
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
            >{m.label}</button>
            {openIdx === i && (
              <div className="menu-dropdown" onMouseEnter={cancelClose} onMouseLeave={scheduleClose}>
                {m.keys.map((k) => {
                  const item = PAGES[k];
                  if (!item) return null;
                  return (
                    <div key={k} className="menu-item" onClick={() => open(k)}>
                      <span className="menu-item-label">{item.label}</span>
                      {item.hint && <span className="menu-item-hint">{item.hint}</span>}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="topbar-right">
        <Ticker />
      </div>
    </div>
  );
}
