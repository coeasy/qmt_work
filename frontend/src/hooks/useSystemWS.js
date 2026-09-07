// 全局系统状态 WS：单例连接，周期推送系统状态（uptime/连接/交易时段），
// 并定期 ping 测延迟。多个组件共享同一连接（通过 listeners 广播快照）。
import { useEffect, useRef, useState } from "react";

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

// 重连语义与 quoteHub 统一：前 10 次指数退避（0.5s→15s 封顶），之后转为 30s
// 固定间隔慢速重连、永不放弃（此前本 hook 10 次后置 offline 等待重挂载，与
// quoteHub 的"10 次后 30s 永不放弃"两套语义并存，系统状态条会永久显示离线）。
const MAX_RETRIES = 10;
const SLOW_INTERVAL = 30000;
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
// 回调经 ref 间接调用：始终使用最新一次渲染的闭包。此前 useEffect(..., []) 只注册
// 首帧回调 —— LimitUp 切板块后事件刷新仍打旧板块、AccountsGrid 关掉 auto 后仍被
// 事件触发刷新（陈旧闭包），均由此修复；调用方无需传 deps。

export function useServerEvents(types = [], onEvent) {
  const typeSet = new Set(types);
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;
  useEffect(() => {
    return subscribeEvent((msg) => {
      if (!typeSet.size || typeSet.has(msg.type)
          || [...typeSet].some((t) => msg.type && msg.type.startsWith(t))) {
        try { cbRef.current(msg); } catch { /* noop */ }
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

function scheduleReconnect() {
  if (reconnectTimer || stopped) return;
  // 达到快速重试上限后不放弃：转 30s 慢速重连（与 quoteHub 一致），状态改 reconnecting
  const delay = retry >= MAX_RETRIES
    ? SLOW_INTERVAL
    : Math.min(0.5 * Math.pow(2, retry), 15);
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
