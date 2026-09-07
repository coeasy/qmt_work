// wsCore —— 共享的 WebSocket 连接治理内核（H4：消除两套重连实现）。
//
// 此前 quoteHub（Provider 版）与 useSystemWS（单例版）各自维护一套
// 重连/心跳/退避逻辑，语义逐渐漂移（一个有僵尸检测、一个没有；
// 心跳间隔 15s/25s 不一致）。本模块把连接治理收敛为一处：
//
//   · 重连永不放弃：前 maxRetries 次指数退避（0.5s → retryCapSec 封顶），
//     超限后转 slowIntervalSec 固定间隔慢速重连（后端恢复即自动接回）
//   · 心跳保活：定时 {action:"ping"}；pong（客户端 ping 应答）与
//     heartbeat（服务端下行帧）都证明链路存活；超过 pingInterval+pongTimeout
//     无任何存活证据 → 判定僵尸连接，主动断开触发重连
//   · 显式停机：stop() 后不再自动重连（后端 shutdown 事件用），
//     再调 connect() 可恢复；destroy() 彻底销毁（组件卸载用）
//   · 状态机：connecting / connected / reconnecting / offline，
//     经 onStateChange 回调通知；重试次数经 getRetries() 读取
//   · 消息解析：JSON 解析在此统一完成，非法 JSON 静默丢弃；
//     pong/heartbeat 在内核内消化（存活证据 + onLatency），不转发给 onMessage
export function createReconnectingSocket({
  url,
  onMessage,
  onOpen,
  onStateChange,
  onLatency,
  maxRetries = 10,
  retryCapSec = 15,
  slowIntervalSec = 30,
  pingIntervalMs = 25000,
  pongTimeoutMs = 10000,
}) {
  let ws = null;
  let retry = 0;
  let reconnectTimer = null;
  let pingTimer = null;
  let lastPongAt = 0;
  let lastPingAt = 0;
  let state = "connecting";
  let stopped = false;

  function setState(next) {
    if (state === next) return;
    state = next;
    try { onStateChange && onStateChange(state); } catch { /* noop */ }
  }

  function clearTimers() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  function scheduleReconnect() {
    if (reconnectTimer || stopped) return;
    setState("reconnecting");
    const n = retry;
    const delay = n >= maxRetries
      ? slowIntervalSec
      : Math.min(0.5 * Math.pow(2, n), retryCapSec);
    retry += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      open();
    }, delay * 1000);
  }

  function startPing() {
    stopPingOnly();
    pingTimer = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      // 僵尸检测：pingInterval + pongTimeout 内无 pong/heartbeat → 主动断开
      if (Date.now() - lastPongAt > pingIntervalMs + pongTimeoutMs) {
        try { ws.close(); } catch { /* noop */ }
        return;
      }
      lastPingAt = Date.now();
      try { ws.send(JSON.stringify({ action: "ping" })); } catch { /* noop */ }
    }, pingIntervalMs);
  }

  function stopPingOnly() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  function open() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    let sock;
    try { sock = new WebSocket(url); } catch {
      setState("offline");
      scheduleReconnect();
      return;
    }
    ws = sock;
    sock.onopen = () => {
      retry = 0;
      lastPongAt = Date.now();
      setState("connected");
      startPing();
      try { onOpen && onOpen(sock); } catch { /* noop */ }
    };
    sock.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "pong") {
        if (lastPingAt) {
          try { onLatency && onLatency(Math.round(performance.now() - lastPingAt)); } catch { /* noop */ }
        }
        lastPongAt = Date.now();
        return;
      }
      if (msg.type === "heartbeat") { lastPongAt = Date.now(); return; }
      try { onMessage && onMessage(msg); } catch { /* noop */ }
    };
    sock.onclose = () => {
      if (ws === sock) ws = null;
      stopPingOnly();
      setState("offline");
      if (!stopped) scheduleReconnect();
    };
    sock.onerror = () => { try { sock.close(); } catch { /* noop */ } };
  }

  // 主动（重）连接：恢复停机标志 + 复位重试计数；已连接则幂等返回。
  function connect() {
    stopped = false;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    clearTimers();
    retry = 0;
    open();
  }

  // 显式停机：不再自动重连（shutdown 事件后调用）。
  function stop() {
    stopped = true;
    clearTimers();
    if (ws) { try { ws.close(); } catch { /* noop */ } ws = null; }
    setState("offline");
  }

  // 彻底销毁：组件卸载用，之后不可再用。
  function destroy() {
    stopped = true;
    clearTimers();
    if (ws) { try { ws.close(); } catch { /* noop */ } ws = null; }
    setState("offline");
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(obj)); } catch { /* noop */ }
    }
  }

  return { connect, stop, destroy, send, getRetries: () => retry };
}
