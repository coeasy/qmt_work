// 全局系统状态 WS：单例连接，周期推送系统状态（uptime/连接/交易时段），
// 并定期 ping 测延迟。多个组件共享同一连接（通过 listeners 广播快照）。
import { useEffect, useState } from "react";

const listeners = new Set();
const brokerHandlers = new Set();
const eventHandlers = new Set();   // P2-2：通用事件订阅（Trade/Algo/LimitUp/Dashboard 等页面）
let ws = null;
let retry = 0;
let reconnectTimer = null;
let pingTimer = null;
let lastLatency = null;
let sysData = null;
let connState = "connecting";

// 最大自动重连次数：防止后端持续宕机/端口异常时无限指数退避重连
// （项目硬性约束：WS 重连必须有上限，杜绝无限重连循环）。
const MAX_RETRIES = 10;
// 显式停机后禁止再自动重连（shutdown 事件后 onclose 不再调度重连）。
let stopped = false;

function emit() {
  const snap = { status: connState, sys: sysData, latency: lastLatency, retries: retry };
  listeners.forEach((cb) => cb(snap));
}

// 订阅券商连接状态事件（broker.connected / broker.disconnected），返回退订函数。
// 供 BrokerContext 近实时更新各连接状态，消除对 /brokers 轮询（15s）的延迟。
export function subscribeBroker(cb) {
  brokerHandlers.add(cb);
  return () => brokerHandlers.delete(cb);
}

// P2-2：通用事件订阅。cb(msg) 收到每条入站 WS 消息（含 type/data），返回退订函数。
// 页面用它监听相关事件近实时刷新，同时保留低频兜底轮询防事件丢失。
export function subscribeEvent(cb) {
  eventHandlers.add(cb);
  connect();
  return () => eventHandlers.delete(cb);
}

// P2-2：便捷 hook —— 监听若干事件类型，命中即回调刷新函数。
// types: 事件类型前缀数组（如 ["order", "trade"]）。列表为空则订阅全部事件。
export function useServerEvents(types = [], onEvent) {
  const typeSet = new Set(types);
  useEffect(() => {
    return subscribeEvent((msg) => {
      if (!typeSet.size || typeSet.has(msg.type)
          || [...typeSet].some((t) => msg.type && msg.type.startsWith(t))) {
        try { onEvent(msg); } catch { /* noop */ }
      }
    });
  }, []);
}

function scheduleReconnect() {
  if (reconnectTimer || stopped) return;
  if (retry >= MAX_RETRIES) {
    // 已达最大重试次数：停止自动重连，置为离线并等待显式触发（组件重挂载 connect()）。
    connState = "offline";
    emit();
    return;
  }
  const delay = Math.min(0.5 * Math.pow(2, retry), 15);
  retry += 1;
  connState = "reconnecting";
  emit();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay * 1000);
}

function stopTimers() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  // 主动（重）连接：复位停机标志与重试计数，允许停机后由组件重挂载/用户操作恢复。
  stopped = false;
  retry = 0;
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
    stopTimers();
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
        // 后端显式停机：停止自动重连 + 心跳，关闭当前连接
        stopped = true;
        stopTimers();
        connState = "offline";
        try { ws && ws.close(); } catch { /* noop */ }
      }
      emit();
    } else if (msg.type === "pong") {
      if (ws && ws._pingAt) {
        lastLatency = Math.round(performance.now() - ws._pingAt);
        emit();
      }
    } else if (typeof msg.type === "string" && msg.type.startsWith("broker.")) {
      // 券商连接状态事件：broker.connected / broker.disconnected
      const data = msg.data || {};
      const event = msg.type.slice("broker.".length);
      brokerHandlers.forEach((cb) => {
        try {
          cb({ conn_id: data.conn_id, connected: event === "connected", event, detail: data });
        } catch { /* noop */ }
      });
    }
    // P2-2：通用事件分发——把每条入站消息广播给事件订阅者（用于近实时刷新而非轮询）
    if (typeof msg === "object" && msg !== null && typeof msg.type === "string") {
      eventHandlers.forEach((cb) => {
        try { cb(msg); } catch { /* noop */ }
      });
    }
  };
  ws.onclose = () => {
    connState = "offline";
    emit();
    // 显式停机后 / 已达最大重试时不再自动重连
    if (!stopped) scheduleReconnect();
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
