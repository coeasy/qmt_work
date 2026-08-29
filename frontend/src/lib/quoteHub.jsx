// QuoteHub —— 全应用唯一的行情 WebSocket 连接中枢。
//
// 背景：改造前每个行情页各自 new WebSocket 并各自指数退避重连，
//   N 个行情 tab = N 条连接 + 切走即断连 + 连接风暴。
// 现在：App 层挂一个 Provider，持有唯一连接；页面通过 useQuotes(codes) 声明式订阅。
//
// 约束（来自后端 app/routes/ws.py）：
//   · 后端支持 action:"subscribe" 与 action:"unsubscribe"（engine.client_subscribe /
//     client_unsubscribe 带引用计数 + 券商侧退订）。本层在本地维护引用计数，
//     当某 code 本地引用清零后，进入 60s 宽限再向服务端发送 unsubscribe，
//     避免翻页 / 短暂切走导致的券商侧订阅抖动（订阅膨胀风险已消除）。
//   · 广播按客户端订阅集合过滤（后端 _client_subscriptions），不区分 conn_id，
//     所以 Hub 不需要 connId 参数；REST 侧的 conn_id 仍由 useKline 单独传。
//
// 快照缓存：新订阅者立刻能拿到最近一次报价，不用等下一个 tick → 切回 tab 无闪白。
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore,
} from "react";

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

const MAX_RETRIES = 10;
const RETRY_CAP_SEC = 15;
// 心跳保活：后端支持 {action:"ping"} → {"type":"pong"}。长时间空闲的 WS 可能被
// 中间层/代理静默掐断而前端毫无感知（onclose 不触发），定时 ping 探活是唯一可靠手段。
const PING_INTERVAL_MS = 25000;   // 每 25s 发一次 ping
const PONG_TIMEOUT_MS = 10000;    // 10s 内无 pong 视为连接已死，主动断开触发重连
// 退订宽限：本地引用清零后 60s 再通知服务端退订，避免翻页 / 短暂切走造成订阅抖动。
const UNSUB_GRACE_MS = 60000;

/* ======================== 外部 store（不可变快照 + 订阅） ======================== */
let snapshot = new Map();        // code -> quote，每次 push 整体替换为新的 Map
const listeners = new Set();

function notify() {
  listeners.forEach((l) => { try { l(); } catch { /* listener 已卸载 */ } });
}

function addListener(l) {
  listeners.add(l);
  return () => { listeners.delete(l); };
}

function getSnapshot() {
  return snapshot;
}

// 写入一条报价（幂等替换该 code 的缓存）。
function pushQuote(q) {
  if (!q || typeof q.code !== "string") return;
  const next = new Map(snapshot);
  next.set(q.code, q);
  snapshot = next;
  notify();
}

function pushBatch(items) {
  if (!Array.isArray(items) || !items.length) return;
  let changed = false;
  const next = new Map(snapshot);
  for (const q of items) {
    if (q && typeof q.code === "string") { next.set(q.code, q); changed = true; }
  }
  if (changed) { snapshot = next; notify(); }
}

// 读取快照中的若干 code（缺失返回 undefined）。
function pickCodes(codes) {
  const out = {};
  for (const c of codes) if (c) out[c] = snapshot.get(c);
  return out;
}

export { pickCodes };

/* ======================== Provider ======================== */
const HubContext = createContext(null);

let _tokenSeq = 0;

export function QuoteHubProvider({ children }) {
  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const timerRef = useRef(null);
  // code -> Set<token>：本地引用计数。token 是订阅句柄。
  const subsRef = useRef(new Map());
  // 已向服务端声明过的 code（幂等集合，断线重连后原样重发）
  const declaredRef = useRef(new Set());
  // code -> timeoutId：本地引用清零后待执行的延迟退订（宽限期内重新订阅则取消）
  const pendingUnsubRef = useRef(new Map());
  const [state, setState] = useState("connecting");
  // 心跳状态（声明在 connect 之前，onopen/onmessage 闭包引用）
  const lastPongRef = useRef(Date.now());
  const stopPingRef = useRef(() => {});

  const declare = useCallback((code) => {
    if (!code) return;
    // 重新订阅：取消仍在宽限期内待执行的延迟退订（避免被立刻退掉）
    const t = pendingUnsubRef.current.get(code);
    if (t) { clearTimeout(t); pendingUnsubRef.current.delete(code); }
    if (declaredRef.current.has(code)) return;
    declaredRef.current.add(code);
    const ws = wsRef.current;
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify({ action: "subscribe", codes: [code] })); } catch { /* noop */ }
    }
  }, []);

  // 订阅：返回一个句柄，页面卸载时用它释放引用。
  const subscribe = useCallback((code) => {
    if (!code) return null;
    const token = { code, id: ++_tokenSeq };
    let set = subsRef.current.get(code);
    if (!set) { set = new Set(); subsRef.current.set(code, set); }
    set.add(token);
    declare(code);
    return token;
  }, [declare]);

  const unsubscribe = useCallback((token) => {
    if (!token) return;
    const set = subsRef.current.get(token.code);
    if (!set) return;
    set.delete(token);
    if (set.size === 0) {
      subsRef.current.delete(token.code);
      // 本地无人消费：进入 60s 宽限，宽限内若重新订阅则取消退订（见 declare）。
      const code = token.code;
      const timer = setTimeout(() => {
        pendingUnsubRef.current.delete(code);
        const ws = wsRef.current;
        if (ws && ws.readyState === 1) {
          try { ws.send(JSON.stringify({ action: "unsubscribe", codes: [code] })); } catch { /* noop */ }
        }
        declaredRef.current.delete(code);
      }, UNSUB_GRACE_MS);
      pendingUnsubRef.current.set(code, timer);
    }
  }, []);

  const redeclareAll = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1) return;
    try { ws.send(JSON.stringify({ action: "subscribe", codes: Array.from(declaredRef.current) })); } catch { /* noop */ }
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (timerRef.current) return;
    setState("reconnecting");
    // 重连永不放弃：前 MAX_RETRIES 次指数退避（0.5s→15s）；
    // 超限后固定 30s 降频无限重试（后端恢复即自动重连，避免「10 次后永久 offline」
    // 导致用户必须手动刷新页面的历史问题）。
    const n = retryRef.current;
    const delay = n >= MAX_RETRIES ? 30 : Math.min(0.5 * Math.pow(2, n), RETRY_CAP_SEC);
    retryRef.current += 1;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      connectRef.current();
    }, delay * 1000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } wsRef.current = null; }
    retryRef.current = 0;
    setState("connecting");
    let ws;
    try { ws = new WebSocket(wsUrl()); }
    catch { setState("offline"); scheduleReconnect(); return; }
    wsRef.current = ws;
    ws.onopen = () => {
      retryRef.current = 0;
      lastPongRef.current = Date.now();
      setState("connected");
      // 重连成功 / 首次连接：把已声明集合整体重发（幂等）
      const codes = Array.from(declaredRef.current);
      if (codes.length) {
        try { ws.send(JSON.stringify({ action: "subscribe", codes })); } catch { /* noop */ }
      }
    };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      // pong（客户端 ping 的应答）与 heartbeat（服务端主动下行帧，B1）都证明链路存活。
      // 服务端心跳的意义：即使客户端没发 ping，也能确认下行通路与代理层未被静默切断。
      if (msg.type === "pong" || msg.type === "heartbeat") { lastPongRef.current = Date.now(); return; }
      if (msg.type === "quotes" && Array.isArray(msg.data?.items)) pushBatch(msg.data.items);
      else if (msg.type === "quotes_replay" && Array.isArray(msg.data?.items)) pushBatch(msg.data.items);
      else if (msg.type === "quote" && msg.data) pushQuote(msg.data);
      else if (msg.type === "snapshot" && msg.data && typeof msg.data === "object") {
        pushBatch(Object.values(msg.data.quotes || {}));
      }
    };
    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null;
      stopPingRef.current();
      setState("offline");
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }, [scheduleReconnect]);

  // 心跳保活：连接打开期间定时 ping；超时无 pong 主动断开（触发 onclose→重连）。
  useEffect(() => {
    let pingTimer = null;
    const tick = () => {
      const ws = wsRef.current;
      if (ws && ws.readyState === 1) {
        if (Date.now() - lastPongRef.current > PING_INTERVAL_MS + PONG_TIMEOUT_MS) {
          // 连接已僵尸化：强制断开，走 onclose → scheduleReconnect
          try { ws.close(); } catch { /* noop */ }
          return;
        }
        try { ws.send(JSON.stringify({ action: "ping" })); } catch { /* noop */ }
      }
    };
    pingTimer = setInterval(tick, PING_INTERVAL_MS);
    stopPingRef.current = () => { clearInterval(pingTimer); };
    return () => { clearInterval(pingTimer); stopPingRef.current = () => {}; };
  }, []);

  // 稳定引用，供 connect() 内部自调用（避免 useCallback 循环依赖）
  const connectRef = useRef(connect);
  useEffect(() => { connectRef.current = connect; }, [connect]);

  useEffect(() => {
    connect();
    return () => {
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
      // 清理所有待执行的延迟退订定时器，避免卸载后向已关闭连接发送
      pendingUnsubRef.current.forEach((t) => clearTimeout(t));
      pendingUnsubRef.current.clear();
      if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } wsRef.current = null; }
    };
  }, [connect]);

  const value = useMemo(
    () => ({ subscribe, unsubscribe, state, declared: declaredRef.current.size }),
    [subscribe, unsubscribe, state],
  );

  return <HubContext.Provider value={value}>{children}</HubContext.Provider>;
}

export function useQuoteHub() {
  const ctx = useContext(HubContext);
  if (!ctx) throw new Error("useQuoteHub 必须在 QuoteHubProvider 内使用");
  return ctx;
}

/* ======================== 页面侧 hook ======================== */
// 声明式订阅：codes 变化时自动增删引用；返回 { quotes, state }。
export function useQuotes(codesIn) {
  const hub = useQuoteHub();
  const { subscribe, unsubscribe, state } = hub;
  const snap = useSyncExternalStore(addListener, getSnapshot);

  // codes 的稳定键：避免数组引用变化导致重复增删引用。
  const codes = useMemo(() => Array.isArray(codesIn) ? codesIn.filter(Boolean) : [], [codesIn]);
  const key = codes.join(",");

  useEffect(() => {
    const list = codes.filter(Boolean).map((c) => subscribe(c));
    return () => { list.forEach((t) => unsubscribe(t)); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, subscribe, unsubscribe]);

  // 快照变化 → 重新挑选需要的 code（O(n)，n 为订阅数，量级很小）
  const quotes = useMemo(() => pickCodes(codes), [snap, codes]);
  return { quotes, state };
}

// 只取单个 code 的便捷版。
export function useQuote(code) {
  const { quotes, state } = useQuotes([code]);
  return { quote: code ? quotes[code] : null, state };
}
