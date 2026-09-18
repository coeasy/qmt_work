import { useEffect, useRef, useState } from "react";
import { Badge } from "@/design/primitives";
import { quoteSocket, type SocketState } from "@/services/ws";
import type { WsMessage } from "@/shared/types";
import { fmtAmount, fmtTime, normalizeCode } from "@/shared/format";
import d from "../../domain.module.css";
import s from "./panels.module.css";

/**
 * 实时成交流面板（WS 逐笔成交 + 大单标志 + 买卖方向）。
 *
 * 从 `DealFeed.tsx` 抽出，改成**受控过滤**：`code` 由调用方给（工作台 = 当前标的，
 * 独立页面 = 用户输入）。原先「自己管一个订阅码」的写法没法嵌进工作台 ——
 * 工作台切标的时成交流必须跟着切，而不是让用户再输一遍代码。
 *
 * 契约与既有实现一致：
 * - WS 复用全局单例 quoteSocket（App 启动时已 connect），不自建系统 WS
 * - 后端成交事件形态：{ type:"deal", data:{ type:"deal_event", data:<realDeal> } }
 *   为兼容历史/未来形变，extractDeal 同时容忍 data.data 与 data 两种形态
 * - 零 mock：WS 未 open 时显式提示「实时通道尚未连接」，不伪造数据
 */

const BIG_DEAL_THRESHOLD = 5_000_000;
const MAX_ROWS = 500;

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
  // ★ 不能把 "order" 当成交：后端 WS 同时有 `order`（委托状态变化，如已报/部成/已撤）
  // 与 `deal`（真实成交）两类事件。把委托混进来，成交流水里会出现「根本没成交」的
  // 记录（价格是委托价、量是委托量），用户据此判断盘口活跃度会被直接误导。
  // 委托状态变化请在委托页看，这里是**成交流水**。
  return (
    t === "deal" ||
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

export function DealFeedPanel({
  code = "",
  autoScrollDefault = true,
  showCode = true,
}: {
  /** 过滤标的；空串 = 查看全部成交流 */
  code?: string;
  autoScrollDefault?: boolean;
  /** 工作台里标的已由头部标明，无需每行重复代码列 */
  showCode?: boolean;
}) {
  const [deals, setDeals] = useState<DealRow[]>([]);
  const [autoScroll, setAutoScroll] = useState(autoScrollDefault);
  const [status, setStatus] = useState<SocketState>(quoteSocket.getState());
  const listRef = useRef<HTMLDivElement>(null);
  const filter = code ? normalizeCode(code) : "";

  useEffect(() => {
    quoteSocket.connect();
    const offMsg = quoteSocket.onMessage((msg: WsMessage) => {
      if (!isDealMessage(msg)) return;
      const row = extractDeal(msg);
      if (!row) return;
      if (filter && normalizeCode(dealCode(row)) !== filter) return;
      setDeals((prev) => [row, ...prev].slice(0, MAX_ROWS));
    });
    const offState = quoteSocket.onState((st) => setStatus(st));
    return () => {
      offMsg();
      offState();
    };
  }, [filter]);

  // 切标的即清空：否则上一个标的的成交会留在列表里，看起来像「新标的的成交」
  useEffect(() => {
    setDeals([]);
  }, [filter]);

  useEffect(() => {
    if (autoScroll && listRef.current) listRef.current.scrollTop = 0;
  }, [deals, autoScroll]);

  const connected = status === "open";

  return (
    <div className={s.fill}>
      <div className={s.miniBar}>
        <Badge tone={connected ? "success" : "warning"}>WS {status}</Badge>
        <span className={d.muted}>{filter ? filter : "全部成交流"}</span>
        <span className={d.spacer} />
        <label style={{ display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />
          自动滚动
        </label>
      </div>

      <div className={d.scroll} ref={listRef}>
        {deals.length === 0 && (
          <div className={d.center} style={{ color: "var(--text-dim)", fontSize: 12 }}>
            {connected
              ? filter
                ? `暂无 ${filter} 成交数据（等待推送中）`
                : "暂无成交数据（等待推送中）"
              : `实时通道尚未连接（${status}），暂无法接收成交推送`}
          </div>
        )}
        {deals.length > 0 && (
          <table className={s.tickTable}>
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>时间</th>
                {showCode && <th style={{ textAlign: "left" }}>代码</th>}
                <th style={{ textAlign: "right" }}>价格</th>
                <th style={{ textAlign: "right" }}>数量</th>
                <th style={{ textAlign: "right" }}>金额</th>
                <th style={{ textAlign: "center" }}>方向</th>
                <th style={{ textAlign: "center" }}>标志</th>
              </tr>
            </thead>
            <tbody>
              {deals.map((row, i) => {
                const price = Number(row.price || 0);
                const volume = Number(row.volume || 0);
                const amount = price * volume;
                const dir = directionOf(row);
                const ts = dealTime(row);
                const timeStr = ts
                  ? fmtTime(typeof ts === "number" && ts < 1e13 ? ts * 1000 : ts)
                  : "—";
                const dirColor =
                  dir === "buy"
                    ? "var(--up)"
                    : dir === "sell"
                      ? "var(--down)"
                      : "var(--text)";
                return (
                  <tr key={row.deal_id || row.order_id || String(i)}>
                    <td className={s.tickTime}>{timeStr}</td>
                    {showCode && <td className={d.mono}>{dealCode(row) || "—"}</td>}
                    <td className={s.tickNum} style={{ color: dirColor }}>
                      {price ? price.toFixed(2) : "—"}
                    </td>
                    <td className={s.tickNum}>{volume ? volume.toLocaleString() : "—"}</td>
                    <td className={s.tickNum}>{amount ? fmtAmount(amount) : "—"}</td>
                    <td className={s.tickDir} style={{ color: dirColor }}>
                      {dir === "buy" ? "买" : dir === "sell" ? "卖" : "中性"}
                    </td>
                    <td className={s.tickDir}>
                      {isBigDeal(row) && <span className={s.bigDeal}>大单</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default DealFeedPanel;
