// 顶部菜单栏（通达信式）：4 个下拉菜单 + 全局搜索 + 指数跑马灯 + 状态区。
// - 菜单 dropdown 浮层（绝对定位，z-index:100），不会被下方元素遮挡
// - 键盘可达：↑↓切换、Enter 打开、Esc 关闭（useHotkeys.js 已处理菜单外键位）
// - 鼠标 hover 打开，离开 180ms 延迟关闭（避免移到 dropdown 时闪烁）
// v2 精简：9 个主菜单 → 4 个（行情/研究/交易/系统）
import { useState, useRef, useEffect } from "react";
import Ticker from "./Ticker.jsx";
import GlobalSearch from "./GlobalSearch.jsx";
import { PAGES, KEY_ALIAS } from "../pagesRegistry.jsx";
import { t } from "../lib/i18n.js";
import { emit } from "../lib/eventBus";

// 4 个主菜单（按功能域分组）：行情 / 研究 / 交易 / 系统
const MENUS = [
  { label: "行情", keys: ["quoteboard", "quote", "mktstructure", "sector_radar", "moneyflow", "deal_feed"] },
  { label: "研究", keys: ["screen", "factor_hub"] },
  { label: "交易", keys: ["trade", "algo", "watchlist"] },
  { label: "系统", keys: ["dashboard", "brokers", "accounts", "sysstatus", "system_log", "settings"] },
];

export default function MenuBar() {
  const [openIdx, setOpenIdx] = useState(-1);
  const closeTimer = useRef(null);

  const scheduleClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setOpenIdx(-1), 180);
  };
  const cancelClose = () => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; } };

  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);

  const open = (key) => { emit("nav", key); setOpenIdx(-1); };

  return (
    <div className="topbar">
      <div className="topbar-left">
        <span className="topbar-brand" title="qmt_work 量化平台">qmt_work</span>
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
      <div className="topbar-right">
        <Ticker />
      </div>
    </div>
  );
}
