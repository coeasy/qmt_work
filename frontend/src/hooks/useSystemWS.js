// 全局系统状态 WS：单例连接，周期推送系统状态（uptime/连接/交易时段），
// 并定期 ping 测延迟。多个组件共享同一连接（通过 listeners 广播快照）。
import { useEffect, useState } from "react";

const listeners = new Set();
let ws = null;
let retry = 0;
let reconnectTimer = null;
let pingTimer = null;
let lastLatency = null;
let sysData = null;
let connState = "connecting";

function emit() {
  const snap = { status: connState, sys: sysData, latency: lastLatency };
  listeners.forEach((cb) => cb(snap));
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = Math.min(0.5 * Math.pow(2, retry), 15);
  retry += 1;
  connState = "reconnecting";
  emit();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay * 1000);
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/api/v1/ws`;
  try {
    ws = new WebSocket(url);
  } catch {
    connState = "offline";
    emit();
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    retry = 0;
    connState = "connected";
    emit();
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws._pingAt = performance.now();
        try { ws.send(JSON.stringify({ action: "ping" })); } catch { /* noop */ }
      }
    }, 15000);
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "system") {
      sysData = msg.data || {};
      if (msg.data && msg.data.event === "shutdown") {
        connState = "offline";
      }
      emit();
    } else if (msg.type === "pong") {
      if (ws && ws._pingAt) {
        lastLatency = Math.round(performance.now() - ws._pingAt);
        emit();
      }
    }
  };
  ws.onclose = () => {
    connState = "offline";
    emit();
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
}

// 首次访问即建立连接（单例）
connect();

export function useSystemStatus() {
  const [snap, setSnap] = useState({ status: connState, sys: sysData, latency: lastLatency });
  useEffect(() => {
    listeners.add(setSnap);
    connect();
    return () => { listeners.delete(setSnap); };
  }, []);
  return snap;
}
