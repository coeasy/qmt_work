// 全局系统状态 WS：单例连接，周期推送系统状态（uptime/连接/交易时段），
// 并定期 ping 测延迟。多个组件共享同一连接（通过 listeners 广播快照）。
// 连接治理（重连退避/心跳保活/僵尸检测/显式停机）全部委托 lib/wsCore（H4），
// 本模块只保留系统域逻辑：system 快照广播、broker 事件、通用事件分发。
import { useEffect, useRef, useState } from "react";
import { createReconnectingSocket } from "../lib/wsCore";
import { WS_EVENTS } from "../shared/events";

const listeners = new Set();
const brokerHandlers = new Set();
const eventHandlers = new Set();   // P2-2：通用事件订阅（Trade/Algo/LimitUp/Dashboard 等页面）
let lastLatency = null;
let sysData = null;
let connState = "connecting";
let retries = 0;

// 显式停机后禁止再自动重连（shutdown 事件后由 core.stop() 保证），core.connect() 可恢复。
const core = createReconnectingSocket({
  url: `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/ws`,
  onStateChange: (s) => { connState = s; emit(); },
  onLatency: (ms) => { lastLatency = ms; emit(); },
  onMessage: onCoreMessage,
});

function emit() {
  retries = core.getRetries();
  const snap = { status: connState, sys: sysData, latency: lastLatency, retries };
  listeners.forEach((cb) => cb(snap));
}

function onCoreMessage(msg) {
  if (msg.type === WS_EVENTS.SYSTEM) {
    sysData = msg.data || {};
    if (msg.data && msg.data.event === "shutdown") {
      // 后端显式停机：停止自动重连 + 心跳，关闭当前连接
      core.stop();
    }
    emit();
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
  core.connect();
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

// 首次访问即建立连接（单例）
core.connect();

export function useSystemStatus() {
  const [snap, setSnap] = useState({ status: connState, sys: sysData, latency: lastLatency });
  useEffect(() => {
    listeners.add(setSnap);
    core.connect();
    return () => { listeners.delete(setSnap); };
  }, []);
  return snap;
}
