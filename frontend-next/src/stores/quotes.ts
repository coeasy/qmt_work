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
      const data = msg.data;
      if (Array.isArray(data)) {
        for (const q of data as Quote[]) st.setQuote(q);
      } else if (data && typeof data === "object") {
        // 快照形态：{ CODE: Quote }
        for (const [code, q] of Object.entries(data as Record<string, Quote>)) {
          st.setQuote({ ...q, code: q.code ?? code });
        }
      }
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
