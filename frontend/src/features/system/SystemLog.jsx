// 系统日志：实时 WS 事件流 + 错误高亮 + 过滤。
// v2 新增独立页面（原 BottomDock 日志 Tab 提升）。
import { useEffect, useState, useRef } from "react";
import { useSystemStatus, useServerEvents } from "../../hooks/useSystemWS.js";

const LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"];
const LEVEL_COLORS = {
  DEBUG: "var(--text-dim)",
  INFO: "var(--text)",
  WARN: "var(--warn)",
  ERROR: "var(--danger)",
};

function inferLevel(text) {
  const s = String(text || "").toUpperCase();
  if (s.includes("ERROR") || s.includes("EXCEPTION") || s.includes("FAIL")) return "ERROR";
  if (s.includes("WARN")) return "WARN";
  if (s.includes("DEBUG") || s.includes("TRACE")) return "DEBUG";
  return "INFO";
}

function summarize(msg) {
  const data = msg?.data;
  let raw = "";
  if (data == null) raw = "";
  else if (typeof data === "string") raw = data;
  else if (typeof data === "number" || typeof data === "boolean") raw = String(data);
  else raw = JSON.stringify(data);
  return raw.length > 500 ? `${raw.slice(0, 500)}…` : raw;
}

export default function SystemLog() {
  const [logs, setLogs] = useState([]);
  const [filter, setFilter] = useState("ALL");
  const [keyword, setKeyword] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const listRef = useRef(null);
  const { status } = useSystemStatus();

  // 后端不单独提供「全量日志 HTTP 拉取」；此页接收系统 WS 全部事件作为运维日志流。
  useServerEvents([], (msg) => {
    const message = summarize(msg) || JSON.stringify(msg || {});
    setLogs((prev) => {
      const entry = {
        time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
        level: inferLevel(message),
        message,
        source: msg?.type || "system",
      };
      return [entry, ...prev].slice(0, 2000);
    });
  });

  useEffect(() => {
    if (autoScroll && listRef.current) listRef.current.scrollTop = 0;
  }, [logs, autoScroll]);

  const filtered = logs.filter((l) => {
    if (filter !== "ALL" && l.level !== filter) return false;
    if (keyword && !`${l.message} ${l.source}`.toLowerCase().includes(keyword.toLowerCase())) return false;
    return true;
  });

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "8px 12px", display: "flex", gap: 8, alignItems: "center", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ padding: "4px 8px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 4, color: "var(--text)" }}>
          <option value="ALL">全部级别</option>
          {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <input
          type="text"
          placeholder="关键字过滤"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          style={{ width: 160, padding: "4px 8px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 4, color: "var(--text)" }}
        />
        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-dim)" }}>
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} /> 自动滚动
        </label>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>WS {status} · {filtered.length} 条</span>
        <button className="btn btn-sm" onClick={() => setLogs([])} style={{ marginLeft: "auto" }}>清空</button>
      </div>
      <div style={{ flex: 1, overflow: "auto", padding: "4px 12px", fontFamily: "monospace", fontSize: 12 }} ref={listRef}>
        {filtered.length === 0 && (
          <div style={{ padding: 24, color: "var(--text-dim)", textAlign: "center" }}>
            {filter === "ALL" && !keyword
              ? (status === "connected" ? "暂无实时事件（等待推送中）" : `实时通道尚未连接（${status}）`)
              : "无匹配日志，调整过滤条件试试"}
          </div>
        )}
        {filtered.map((l, i) => (
          <div key={i} style={{ padding: "2px 0", display: "flex", gap: 8, borderBottom: "1px solid var(--border)", opacity: l.level === "ERROR" ? 1 : l.level === "WARN" ? 0.9 : 0.7 }}>
            <span style={{ color: "var(--text-dim)", width: 70, flexShrink: 0 }}>{l.time}</span>
            <span style={{ color: LEVEL_COLORS[l.level] || "var(--text)", fontWeight: l.level === "ERROR" || l.level === "WARN" ? 600 : 400, width: 50, flexShrink: 0 }}>{l.level}</span>
            <span style={{ color: l.level === "ERROR" ? "var(--danger)" : l.level === "WARN" ? "var(--warn)" : "var(--text)" }} title={l.message}>{l.message}</span>
            {l.source && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>[{l.source}]</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
