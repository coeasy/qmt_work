// 底部综合栏（通达信式）：可折叠多 tab（自选股/板块/预警/成交/日志）。
import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useSystemStatus, useServerEvents } from "../hooks/useSystemWS.js";

const TABS = [
  { key: "watch", label: "自选股" },
  { key: "sectors", label: "板块" },
  { key: "alerts", label: "预警" },
  { key: "trades", label: "成交" },
  { key: "log", label: "日志" },
];

const WATCH_KEY = "qmt_work.watchlist.v1";

export default function BottomDock() {
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState("watch");
  const [sectors, setSectors] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [trades, setTrades] = useState([]);
  const [logs, setLogs] = useState([]);
  const [watch, setWatch] = useState(() => {
    try { return JSON.parse(localStorage.getItem(WATCH_KEY) || "[]"); } catch { return []; }
  });
  const [watchInput, setWatchInput] = useState("");
  const { status } = useSystemStatus();

  useEffect(() => {
    if (tab === "sectors") {
      api.sectors().then(setSectors).catch(() => setSectors([]));
    } else if (tab === "alerts") {
      api.alertRules().then(setAlerts).catch(() => setAlerts([]));
    }
  }, [tab]);

  // 实时事件路由：成交进 trades，全部事件进 log（仅当前 tab 时更新状态，省渲染）
  const tabRef = useRef(tab);
  tabRef.current = tab;
  useServerEvents([], (msg) => {
    const t = msg.type || "";
    if (tabRef.current === "trades" && (t === "trade" || t.startsWith("trade") || t === "fill")) {
      setTrades((prev) => [{ ...msg.data, _t: Date.now() }, ...prev].slice(0, 50));
    }
    if (tabRef.current === "log") {
      setLogs((prev) => [{ type: t, ts: new Date().toLocaleTimeString(), raw: msg }, ...prev].slice(0, 60));
    }
  });

  function addWatch() {
    const c = watchInput.trim().toUpperCase();
    if (!c) return;
    const nw = watch.includes(c) ? watch : [...watch, c];
    setWatch(nw);
    try { localStorage.setItem(WATCH_KEY, JSON.stringify(nw)); } catch { /* noop */ }
    setWatchInput("");
  }
  function rmWatch(c) {
    const nw = watch.filter((x) => x !== c);
    setWatch(nw);
    try { localStorage.setItem(WATCH_KEY, JSON.stringify(nw)); } catch { /* noop */ }
  }
  const openQuote = (code) =>
    window.dispatchEvent(new CustomEvent("nav", { detail: "quote" }));

  return (
    <div className={`dock ${open ? "" : "collapsed"}`}>
      <div className="dock-bar">
        <div className="dock-tabs">
          {TABS.map((t) => (
            <span
              key={t.key}
              className={`dock-tab ${tab === t.key ? "active" : ""}`}
              onClick={() => { setTab(t.key); setOpen(true); }}
            >{t.label}</span>
          ))}
        </div>
        <span className="dock-toggle" onClick={() => setOpen((o) => !o)}>
          {open ? "▾ 收起" : "▴ 展开"}
        </span>
      </div>
      {open && (
        <div className="dock-body">
          {tab === "watch" && (
            <div className="dock-watch">
              <div className="dock-watch-input">
                <input
                  placeholder="添加自选代码，如 600519.SH"
                  value={watchInput}
                  onChange={(e) => setWatchInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addWatch()}
                />
                <button className="btn-sm" onClick={addWatch}>添加</button>
              </div>
              <div className="dock-watch-list">
                {watch.length === 0
                  ? <span className="muted">暂无自选（演示：本地存储，可在命令面板/报价牌快速添加）</span>
                  : watch.map((c) => (
                    <span className="dock-watch-chip" key={c} onClick={() => openQuote(c)}>
                      {c}<span className="dock-watch-x" onClick={(e) => { e.stopPropagation(); rmWatch(c); }}>×</span>
                    </span>
                  ))}
              </div>
            </div>
          )}
          {tab === "sectors" && (
            <div className="dock-sectors">
              {sectors.length === 0
                ? <span className="muted">加载中…</span>
                : sectors.slice(0, 60).map((s, i) => (
                  <span className="dock-sector-chip" key={i}>{s.name || s.sector || JSON.stringify(s).slice(0, 20)}</span>
                ))}
            </div>
          )}
          {tab === "alerts" && (
            <table className="dock-table">
              <thead><tr><th>名称</th><th>条件</th><th>状态</th></tr></thead>
              <tbody>
                {alerts.length === 0
                  ? <tr><td colSpan={3} className="muted">暂无告警规则</td></tr>
                  : alerts.map((a, i) => (
                    <tr key={i}><td>{a.name || a.id}</td><td>{a.condition || ""}</td><td>{a.enabled ? "启用" : "停用"}</td></tr>
                  ))}
              </tbody>
            </table>
          )}
          {tab === "trades" && (
            <table className="dock-table">
              <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>价格</th><th>量</th></tr></thead>
              <tbody>
                {trades.length === 0
                  ? <tr><td colSpan={5} className="muted">暂无成交（需连接券商）</td></tr>
                  : trades.map((t, i) => (
                    <tr key={i}>
                      <td>{t._t ? new Date(t._t).toLocaleTimeString() : ""}</td>
                      <td className="code">{t.code || ""}</td>
                      <td className={t.side === "buy" ? "up" : "down"}>{t.side === "buy" ? "买" : "卖"}</td>
                      <td>{t.price != null ? Number(t.price).toFixed(2) : ""}</td>
                      <td>{t.volume != null ? t.volume : ""}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
          {tab === "log" && (
            <div className="dock-log">
              {logs.length === 0
                ? <span className="muted">监听 WS 事件中…（当前 {status}）</span>
                : logs.map((l, i) => (
                  <div className="dock-log-line" key={i}>
                    <span className="dock-log-ts">{l.ts}</span>
                    <span className="dock-log-type">{l.type}</span>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
