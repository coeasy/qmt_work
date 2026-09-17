import { create } from "zustand";
import type { Quote, WsMessage } from "@/shared/types";
import { quoteSocket, type SocketState } from "@/services/ws";

/**
 * 行情 store。
 *
 * ★ 关键设计（修复旧前端缺陷）：订阅聚合。
 * 旧实现中每个 Tab 各自调用 useQuotes，最多产生 24 个 WS 订阅；
 * 本实现以标的为键做引用计数：
 *   - 第一个订阅者 → 向服务端 subscribe
 *   - 后续订阅者 → 仅增加计数，不再下发
 *   - 最后一个释放者 → 向服务端 unsubscribe
 * 因此「打开 10 个含 600519 的 Tab」也只占用 1 个服务端订阅。
 */

interface QuotesState {
  quotes: Record<string, Quote>;
  refs: Record<string, number>;
  socketState: SocketState;
  lastSeq: number;

  acquire: (codes: string[]) => void;
  release: (codes: string[]) => void;
  setQuote: (q: Quote) => void;
  clear: () => void;
  /** 供测试与调试：当前活跃订阅标的 */
  activeCodes: () => string[];
}

let initialized = false;

export const useQuotesStore = create<QuotesState>((set, get) => ({
  quotes: {},
  refs: {},
  socketState: "idle",
  lastSeq: 0,

  acquire(codes) {
    const { refs } = get();
    const next = { ...refs };
    const fresh: string[] = [];
    for (const c of codes) {
      const cur = next[c] ?? 0;
      next[c] = cur + 1;
      if (cur === 0) fresh.push(c);
    }
    set({ refs: next });
    if (fresh.length > 0) {
      quoteSocket.connect();
      quoteSocket.addCodes(fresh);
    }
  },

  release(codes) {
    const { refs } = get();
    const next = { ...refs };
    const gone: string[] = [];
    for (const c of codes) {
      const cur = next[c] ?? 0;
      if (cur <= 1) {
        delete next[c];
        if (cur === 1) gone.push(c);
      } else {
        next[c] = cur - 1;
      }
    }
    set({ refs: next });
    if (gone.length > 0) quoteSocket.removeCodes(gone);
  },

  setQuote(q) {
    if (!q?.code) return;
    set((s) => ({ quotes: { ...s.quotes, [q.code]: { ...s.quotes[q.code], ...q } } }));
  },

  clear() {
    set({ quotes: {}, refs: {} });
  },

  activeCodes() {
    return Object.keys(get().refs);
  },
}));

/**
 * 从 WS 帧里取出行情数组。
 *
 * ★ 后端有**两种**帧形状，历史上前端只认「`data` 是数组或对象」这一种，
 *   于是两种帧都解析不出来 —— 表现是「行情通道已连接，但价格/涨跌幅永远是 --」，
 *   既不报错也没有兜底，极难定位：
 *     - 全量快照：`{"type":"snapshot", "quotes": { CODE: Quote }}`
 *     - 增量广播：`{"type":"quotes",  "data": {"items": [Quote, ...]}}`
 *   契约在 backend/sync/__init__.py 的 `send_full_snapshot` / `broadcast`，
 *   改任一侧都要同步这里（护栏：tests/quoteFrames.test.ts）。
 */
export function extractQuotes(msg: WsMessage): Quote[] {
  const out: Quote[] = [];
  // 自守卫：本函数只认行情帧。调用方虽已按 type 过滤，但函数自己守住这条
  // 边界后，将来新增的帧类型（order / account / risk…）不可能被误吞成行情。
  if (msg.type !== "snapshot" && msg.type !== "quotes") return out;

  if (msg.type === "snapshot") {
    const snap = msg.quotes;
    if (snap && typeof snap === "object" && !Array.isArray(snap)) {
      for (const [code, q] of Object.entries(snap)) {
        if (q && typeof q === "object") out.push({ ...q, code: q.code ?? code });
      }
    }
    return out;
  }

  const data = msg.data as unknown;
  const list = Array.isArray(data)
    ? (data as Quote[])
    : data && typeof data === "object" && Array.isArray((data as { items?: unknown }).items)
      ? (data as { items: Quote[] }).items
      : null;
  if (list) {
    for (const q of list) {
      if (q && typeof q === "object" && q.code) out.push(q);
    }
    return out;
  }

  // 兼容 `data` 直接是 `{ CODE: Quote }` 的形态
  if (data && typeof data === "object") {
    for (const [code, q] of Object.entries(data as Record<string, Quote>)) {
      if (q && typeof q === "object") out.push({ ...q, code: q.code ?? code });
    }
  }
  return out;
}

/** 应用启动时调用一次：接管 socket 消息 → store，并同步连接状态 */
export function initQuotePipeline(): () => void {
  if (initialized) return () => undefined;
  initialized = true;

  const offMsg = quoteSocket.onMessage((msg: WsMessage) => {
    const st = useQuotesStore.getState();
    if (msg.seq !== undefined) {
      useQuotesStore.setState({ lastSeq: msg.seq });
    }
    if (msg.type === "quotes" || msg.type === "snapshot") {
      for (const q of extractQuotes(msg)) st.setQuote(q);
    }
  });

  const offState = quoteSocket.onState((s) => {
    useQuotesStore.setState({ socketState: s });
  });

  quoteSocket.connect();

  return () => {
    offMsg();
    offState();
    initialized = false;
  };
}

/** 订阅一组标的（组件挂载时 acquire、卸载时 release） */
export function acquireQuotes(codes: string[]): void {
  if (codes.length === 0) return;
  useQuotesStore.getState().acquire(codes);
}

export function releaseQuotes(codes: string[]): void {
  if (codes.length === 0) return;
  useQuotesStore.getState().release(codes);
}
