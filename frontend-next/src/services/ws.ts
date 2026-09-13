import type { WsMessage } from "@/shared/types";
import { getApiKey } from "./http";

/**
 * WebSocket 传输层。
 *
 * 职责边界：只负责「连接生命周期 + 消息收发 + 断线重连」，不做订阅聚合。
 * 订阅聚合（refcount）在 stores/quotes.ts —— 这样多个组件订阅同一标的
 * 只产生一次服务端订阅，修掉旧前端「每个 Tab 各自订阅、最多 24 个订阅」的浪费。
 *
 * 后端协议（sync/__init__.py:588）：
 *   客户端 → {"action":"subscribe"|"unsubscribe","codes":[...]}
 *   客户端 → {"action":"ping"}  ⇒ 服务端 {"type":"pong","seq":N}
 *   服务端 → {"type":<event>,"data":...,"seq":N}
 *   连接建立后服务端先推一帧全量快照，并对订阅代码补发最近 30s 行情。
 */

export type SocketState = "idle" | "connecting" | "open" | "closed";

type Handler = (msg: WsMessage) => void;
type StateHandler = (state: SocketState) => void;

const MAX_BACKOFF = 30_000;
const HEARTBEAT_MS = 25_000;

class QuoteSocket {
  private ws: WebSocket | null = null;
  private state: SocketState = "idle";
  private handlers = new Set<Handler>();
  private stateHandlers = new Set<StateHandler>();
  private retry = 0;
  private reconnectTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private manualClose = false;

  /** 当前应保持的订阅集合（重连后自动恢复） */
  private desired = new Set<string>();

  getState(): SocketState {
    return this.state;
  }

  onMessage(h: Handler): () => void {
    this.handlers.add(h);
    return () => this.handlers.delete(h);
  }

  onState(h: StateHandler): () => void {
    this.stateHandlers.add(h);
    return () => this.stateHandlers.delete(h);
  }

  private setState(s: SocketState): void {
    if (this.state === s) return;
    this.state = s;
    for (const h of this.stateHandlers) h(s);
  }

  connect(): void {
    if (this.state === "open" || this.state === "connecting") return;
    this.manualClose = false;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const token = getApiKey();
    const url = `${proto}//${location.host}/api/v1/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;

    this.setState("connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.retry = 0;
      this.setState("open");
      // 重连后恢复订阅集合（服务端连接级订阅随旧连接失效）
      if (this.desired.size > 0) {
        this.send({ action: "subscribe", codes: [...this.desired] });
      }
      this.startHeartbeat();
    };

    ws.onmessage = (ev) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(String(ev.data)) as WsMessage;
      } catch {
        return;
      }
      for (const h of this.handlers) h(msg);
    };

    ws.onerror = () => {
      /* onclose 会跟随触发，统一在 onclose 处理重连 */
    };

    ws.onclose = () => {
      this.stopHeartbeat();
      this.ws = null;
      this.setState("closed");
      if (!this.manualClose) this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const delay = Math.min(1000 * 2 ** this.retry, MAX_BACKOFF);
    this.retry += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(() => {
      this.send({ action: "ping" });
    }, HEARTBEAT_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private send(payload: unknown): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(payload));
    return true;
  }

  /** 由 quotes store 调用：把标的加入期望订阅集合并下发 */
  addCodes(codes: string[]): void {
    const fresh = codes.filter((c) => !this.desired.has(c));
    for (const c of fresh) this.desired.add(c);
    if (fresh.length > 0) this.send({ action: "subscribe", codes: fresh });
  }

  /** 由 quotes store 调用：refcount 归零后真正退订 */
  removeCodes(codes: string[]): void {
    const gone = codes.filter((c) => this.desired.has(c));
    for (const c of gone) this.desired.delete(c);
    if (gone.length > 0) this.send({ action: "unsubscribe", codes: gone });
  }

  /** 供测试与调试：当前期望订阅集合 */
  desiredCodes(): string[] {
    return [...this.desired];
  }

  close(): void {
    this.manualClose = true;
    this.stopHeartbeat();
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.setState("idle");
  }
}

export const quoteSocket = new QuoteSocket();
