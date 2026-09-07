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
import { createReconnectingSocket } from "./wsCore";

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

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
  const coreRef = useRef(null);
  // code -> Set<token>：本地引用计数。token 是订阅句柄。
  const subsRef = useRef(new Map());
  // 已向服务端声明过的 code（幂等集合，断线重连后原样重发）
  const declaredRef = useRef(new Set());
  // code -> timeoutId：本地引用清零后待执行的延迟退订（宽限期内重新订阅则取消）
  const pendingUnsubRef = useRef(new Map());
  const [state, setState] = useState("connecting");

  // 连接治理（重连/心跳/退避/僵尸检测）全部委托 wsCore（H4：单一实现）。
  // 本组件只保留行情域逻辑：订阅引用计数 + 60s 退订宽限 + 重连后重发声明。
  if (coreRef.current === null) {
    coreRef.current = createReconnectingSocket({
      url: wsUrl(),
      onStateChange: setState,
      // 重连成功 / 首次连接：把已声明集合整体重发（幂等）
      onOpen: () => {
        const codes = Array.from(declaredRef.current);
        if (codes.length) coreRef.current.send({ action: "subscribe", codes });
      },
      onMessage: (msg) => {
        if (msg.type === "quotes" && Array.isArray(msg.data?.items)) pushBatch(msg.data.items);
        else if (msg.type === "quotes_replay" && Array.isArray(msg.data?.items)) pushBatch(msg.data.items);
        else if (msg.type === "quote" && msg.data) pushQuote(msg.data);
        else if (msg.type === "snapshot" && msg.data && typeof msg.data === "object") {
          pushBatch(Object.values(msg.data.quotes || {}));
        }
      },
    });
  }

  const declare = useCallback((code) => {
    if (!code) return;
    // 重新订阅：取消仍在宽限期内待执行的延迟退订（避免被立刻退掉）
    const t = pendingUnsubRef.current.get(code);
    if (t) { clearTimeout(t); pendingUnsubRef.current.delete(code); }
    if (declaredRef.current.has(code)) return;
    declaredRef.current.add(code);
    coreRef.current.send({ action: "subscribe", codes: [code] });
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
        coreRef.current.send({ action: "unsubscribe", codes: [code] });
        declaredRef.current.delete(code);
      }, UNSUB_GRACE_MS);
      pendingUnsubRef.current.set(code, timer);
    }
  }, []);

  useEffect(() => {
    coreRef.current.connect();
    return () => {
      // 清理所有待执行的延迟退订定时器，避免卸载后向已关闭连接发送
      pendingUnsubRef.current.forEach((t) => clearTimeout(t));
      pendingUnsubRef.current.clear();
      coreRef.current.destroy();
    };
  }, []);

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
