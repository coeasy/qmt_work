import { useEffect, useRef, useState } from "react";
import { Button } from "@/design/primitives";
import { quoteSocket, type SocketState } from "@/services/ws";
import type { WsMessage } from "@/shared/types";
import { fmtAmount, fmtTime, normalizeCode } from "@/shared/format";
import s from "../domain.module.css";

/**
 * 成交明细：实时逐笔成交 + 大单标志 + 买卖方向。
 *
 * 移植自旧 frontend/features/market/DealFeed.jsx，按 frontend-next 约定重写：
 * - WS 直接复用全局单例 quoteSocket（App 启动时已 connect），不再自建系统 WS
 * - 后端成交事件形态：{ type:"deal", data:{ type:"deal_event", data:<realDeal> } }
 *   为兼容历史/未来形变，extractDeal 同时容忍 data.data 与 data 两种形态
 * - 设计令牌全部走 tokens.css（--bg-3 / --border / --up / --down / --warning / --text*）
 * - 零 mock：离线未连接券商时 WS 状态非 open，页面显式提示「实时通道尚未连接」
 */

const BIG_DEAL_THRESHOLD = 5_000_000;

interface DealRow {
  code?: string;
  symbol?: string;
  stock_code?: string;
  price?: number;
  volume?: number;
  amount?: number;
  side?: unknown;
  direction?: unknown;
  type?: unknown;
  timestamp?: unknown;
  ts?: unknown;
  created_at?: unknown;
  time?: unknown;
  deal_id?: string;
  order_id?: string;
  _t: number;
}

function isDealMessage(msg: WsMessage): boolean {
  const t = msg.type;
  if (typeof t !== "string") return false;
  return (
    t === "deal" ||
    t === "order" ||
    t === "trade" ||
    t === "fill" ||
    t.startsWith("deal") ||
    t.startsWith("trade") ||
    t.startsWith("fill")
  );
}

function extractDeal(msg: WsMessage): DealRow | null {
  const payload = msg.data;
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  const inner: Record<string, unknown> =
    p.data && typeof p.data === "object" ? (p.data as Record<string, unknown>) : p;
  if (Object.keys(inner).length === 0) return null;
  return { ...inner, _t: Date.now() } as DealRow;
}

function dealCode(d: DealRow): string {
  return d.code ?? d.symbol ?? d.stock_code ?? "";
}

function dealTime(d: DealRow): string | number | undefined {
  const v = d.timestamp ?? d.ts ?? d.created_at ?? d.time;
  return typeof v === "string" || typeof v === "number" ? v : undefined;
}

function isBigDeal(d: DealRow): boolean {
  const price = Number(d.price || 0);
  const volume = Number(d.volume || 0);
  return price * volume >= BIG_DEAL_THRESHOLD;
}

function directionOf(d: DealRow): "buy" | "sell" | "neutral" {
  const raw = d.side ?? d.direction ?? d.type;
  if (raw === "buy" || raw === 1 || raw === "BUY" || raw === "B") return "buy";
  if (raw === "sell" || raw === -1 || raw === "SELL" || raw === "S") return "sell";
  return "neutral";
}

export default function DealFeed(_props: unknown) {
  const [code, setCode] = useState("");
  const [subscribedCode, setSubscribedCode] = useState("");
  const [deals, setDeals] = useState<DealRow[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [status, setStatus] = useState<SocketState>(quoteSocket.getState());
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    quoteSocket.connect();
    const offMsg = quoteSocket.onMessage((msg: WsMessage) => {
      if (!isDealMessage(msg)) return;
      const d = extractDeal(msg);
      if (!d) return;
      const sc = subscribedCode ? normalizeCode(subscribedCode) : "";
      if (sc && normalizeCode(dealCode(d)) !== sc) return;
      setDeals((prev) => [d, ...prev].slice(0, 500));
    });
    const offState = quoteSocket.onState((st) => setStatus(st));
    return () => {
      offMsg();
      offState();
    };
  }, [subscribedCode]);

  useEffect(() => {
    if (autoScroll && listRef.current) listRef.current.scrollTop = 0;
  }, [deals, autoScroll]);

  const handleSubscribe = () => {
    const raw = code.trim();
    const c = normalizeCode(raw);
    if (/^\d{6}(\.(SH|SZ|BJ))?$/.test(c) || /^\d{6}\.[A-Z]{2}$/.test(raw.toUpperCase())) {
      setSubscribedCode(c);
      setDeals([]);
    }
  };

  const connected = status === "open";

  return (
    <div className={s.page} style={{ padding: 0 }}>
      <div className={s.toolbar} style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)" }}>
        <input
          className={s.mono}
          placeholder="6位 或 600519.SH"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSubscribe()}
          style={{
            width: 160,
            padding: "4px 8px",
            background: "var(--bg-3)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text)",
          }}
        />
        <Button size="sm" variant="default" onClick={handleSubscribe}>
          订阅
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            setSubscribedCode("");
            setDeals([]);
          }}
        >
          全部
        </Button>
        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-dim)" }}>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />{" "}
          自动滚动
        </label>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
          WS {status} · {subscribedCode ? `已订阅: ${subscribedCode}` : "查看全部成交流"}
        </span>
      </div>

      <div className={s.scroll} ref={listRef} style={{ padding: "4px 12px" }}>
        {deals.length === 0 && (
          <div className={s.center} style={{ color: "var(--text-dim)" }}>
            {connected
              ? subscribedCode
                ? `暂无 ${subscribedCode} 成交数据（等待推送中）`
                : "暂无成交数据（等待推送中）"
              : `实时通道尚未连接（${status}），暂无法接收成交推送`}
          </div>
        )}
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
              <th style={{ padding: "4px 6px" }}>时间</th>
              <th>代码</th>
              <th>价格</th>
              <th>数量</th>
              <th>金额</th>
              <th>方向</th>
              <th>标志</th>
            </tr>
          </thead>
          <tbody>
            {deals.map((d, i) => {
              const price = Number(d.price || 0);
              const volume = Number(d.volume || 0);
              const amount = price * volume;
              const dir = directionOf(d);
              const big = isBigDeal(d);
              const ts = dealTime(d);
              const timeStr = ts
                ? fmtTime(typeof ts === "number" && ts < 1e13 ? ts * 1000 : ts)
                : "-";
              const dirColor =
                dir === "buy"
                  ? "var(--up)"
                  : dir === "sell"
                    ? "var(--down)"
                    : "var(--text)";
              return (
                <tr key={(d.deal_id || d.order_id || String(i))} style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "3px 6px", color: "var(--text-dim)" }}>{timeStr}</td>
                  <td className={s.mono}>{dealCode(d) || "-"}</td>
                  <td style={{ color: dirColor }}>{price ? price.toFixed(2) : "-"}</td>
                  <td>{volume ? volume.toLocaleString() : "-"}</td>
                  <td>{amount ? fmtAmount(amount) : (d.amount ?? "-")}</td>
                  <td>
                    <span style={{ color: dirColor }}>
                      {dir === "buy" ? "买" : dir === "sell" ? "卖" : "中性"}
                    </span>
                  </td>
                  <td>
                    {big ? (
                      <span style={{ color: "var(--warning)", fontWeight: 600 }}>大单</span>
                    ) : (
                      ""
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
