import { useEffect, useRef, useState } from "react";
import { Button } from "@/design/primitives";
import { quoteSocket, type SocketState } from "@/services/ws";
import { wsEventLabel } from "@/services/wsEvents";
import type { WsMessage } from "@/shared/types";
import s from "../domain.module.css";

/**
 * 系统日志：实时 WS 事件流 + 错误高亮 + 过滤。
 *
 * 移植自旧 frontend/features/system/SystemLog.jsx。后端不单独提供全量日志 HTTP
 * 拉取，此处把系统 WS 的全部事件作为运维日志流；离线未连接时显式提示。
 * 设计令牌走 tokens.css（--danger / --warning / --text* / --border）。
 */

const LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"] as const;
type Level = (typeof LEVELS)[number];

const LEVEL_COLORS: Record<Level, string> = {
  DEBUG: "var(--text-dim)",
  INFO: "var(--text)",
  WARN: "var(--warning)",
  ERROR: "var(--danger)",
};

interface LogEntry {
  time: string;
  level: Level;
  message: string;
  source: string;
}

function inferLevel(text: string): Level {
  const u = text.toUpperCase();
  if (u.includes("ERROR") || u.includes("EXCEPTION") || u.includes("FAIL")) return "ERROR";
  if (u.includes("WARN")) return "WARN";
  if (u.includes("DEBUG") || u.includes("TRACE")) return "DEBUG";
  return "INFO";
}

function summarize(msg: WsMessage): string {
  const data = msg.data;
  let raw: string;
  if (data === null || data === undefined) raw = "";
  else if (typeof data === "string") raw = data;
  else if (typeof data === "number" || typeof data === "boolean") raw = String(data);
  else raw = JSON.stringify(data);
  return raw.length > 500 ? `${raw.slice(0, 500)}…` : raw;
}

export default function SystemLog(_props: unknown) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [filter, setFilter] = useState<Level | "ALL">("ALL");
  const [keyword, setKeyword] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [status, setStatus] = useState<SocketState>(quoteSocket.getState());
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    quoteSocket.connect();
    const offMsg = quoteSocket.onMessage((msg: WsMessage) => {
      const message = summarize(msg) || JSON.stringify(msg || {});
      setLogs((prev) => {
        const entry: LogEntry = {
          time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
          level: inferLevel(message),
          message,
          source: wsEventLabel(msg),
        };
        return [entry, ...prev].slice(0, 2000);
      });
    });
    const offState = quoteSocket.onState((st) => setStatus(st));
    return () => {
      offMsg();
      offState();
    };
  }, []);

  useEffect(() => {
    if (autoScroll && listRef.current) listRef.current.scrollTop = 0;
  }, [logs, autoScroll]);

  const filtered = logs.filter((l) => {
    if (filter !== "ALL" && l.level !== filter) return false;
    if (keyword && !`${l.message} ${l.source}`.toLowerCase().includes(keyword.toLowerCase()))
      return false;
    return true;
  });

  const connected = status === "open";

  return (
    <div className={s.page} style={{ padding: 0 }}>
      <div className={s.toolbar} style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)" }}>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as Level | "ALL")}
          style={{
            padding: "4px 8px",
            background: "var(--bg-3)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text)",
          }}
        >
          <option value="ALL">全部级别</option>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <input
          placeholder="关键字过滤"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          style={{
            width: 170,
            padding: "4px 8px",
            background: "var(--bg-3)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text)",
          }}
        />
        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-dim)" }}>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />{" "}
          自动滚动
        </label>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
          WS {status} · {filtered.length} 条
        </span>
        <Button
          size="sm"
          variant="ghost"
          style={{ marginLeft: "auto" }}
          onClick={() => setLogs([])}
        >
          清空
        </Button>
      </div>

      <div className={s.scroll} ref={listRef} style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>
        {filtered.length === 0 && (
          <div className={s.center} style={{ color: "var(--text-dim)" }}>
            {filter === "ALL" && !keyword
              ? connected
                ? "暂无实时事件（等待推送中）"
                : `实时通道尚未连接（${status}）`
              : "无匹配日志，调整过滤条件试试"}
          </div>
        )}
        {filtered.map((l, i) => (
          <div
            key={i}
            style={{
              padding: "2px 12px",
              display: "flex",
              gap: 8,
              borderBottom: "1px solid var(--border)",
              opacity: l.level === "ERROR" ? 1 : l.level === "WARN" ? 0.9 : 0.7,
            }}
          >
            <span style={{ color: "var(--text-dim)", width: 70, flexShrink: 0 }}>{l.time}</span>
            <span
              style={{
                color: LEVEL_COLORS[l.level],
                fontWeight: l.level === "ERROR" || l.level === "WARN" ? 600 : 400,
                width: 50,
                flexShrink: 0,
              }}
            >
              {l.level}
            </span>
            <span
              style={{
                color:
                  l.level === "ERROR" ? "var(--danger)" : l.level === "WARN" ? "var(--warning)" : "var(--text)",
              }}
              title={l.message}
            >
              {l.message}
            </span>
            {l.source && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>[{l.source}]</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
