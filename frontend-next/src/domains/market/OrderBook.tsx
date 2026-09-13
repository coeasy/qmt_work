import { useMemo, useState } from "react";
import { Badge, Button, EmptyState, Input, Panel, Spinner } from "@/design/primitives";
import { marketApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, fmtVolume, normalizeCode, toneColor } from "@/shared/format";
import s from "../domain.module.css";

/**
 * 盘口 / 逐笔成交。
 *
 * ★ 契约要点：
 *   - 五档来自 /market/quote 的 bid/ask 与 bid_vol/ask_vol 数组（长度可能不足 5，按实际渲染）
 *   - 逐笔走 /market/l2（券商 get_l2_transactions），**券商未连接时返回 503**；
 *     TDX 公共行情不提供逐笔，故本页在无券商时逐笔区域明确提示而非空白
 *   - 行情快照本身由 WS 订阅驱动（useQuoteSubscription），无需轮询
 *
 * 零 mock：任一档位/逐笔缺失时显示「—」，不用 0 填充。
 */
export function OrderBook() {
  const [code, setCode] = useState("000001.SZ");
  const [l2Count, setL2Count] = useState("50");

  const normCode = useMemo(() => normalizeCode(code), [code]);
  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  const l2 = useAsync(() => marketApi.l2(normCode, Number(l2Count) || 50), [normCode, l2Count]);

  const bids = quote?.bid ?? [];
  const asks = quote?.ask ?? [];
  const bidVols = quote?.bid_vol ?? [];
  const askVols = quote?.ask_vol ?? [];

  /** 盘口按「卖5→卖1 / 买1→买5」纵向排列（终端惯例） */
  const rows = useMemo(() => {
    const out: Array<{ label: string; price?: number; vol?: number; side: "ask" | "bid" | "mid" }> = [];
    for (let i = 4; i >= 0; i--) {
      out.push({ label: `卖${i + 1}`, price: asks[i], vol: askVols[i], side: "ask" });
    }
    out.push({ label: "最新", price: quote?.price, vol: undefined, side: "mid" });
    for (let i = 0; i < 5; i++) {
      out.push({ label: `买${i + 1}`, price: bids[i], vol: bidVols[i], side: "bid" });
    }
    return out;
  }, [asks, askVols, bids, bidVols, quote?.price]);

  const maxVol = Math.max(
    1,
    ...bidVols.filter((v) => typeof v === "number"),
    ...askVols.filter((v) => typeof v === "number"),
  );

  const ticks = l2.data ?? [];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Input value={code} onChange={(e) => setCode(e.target.value)} mono style={{ width: 150 }} placeholder="代码" />
        <Input value={l2Count} onChange={(e) => setL2Count(e.target.value)} mono style={{ width: 70 }} />
        <Button size="sm" variant="ghost" onClick={() => void l2.reload()}>
          刷新逐笔
        </Button>
        <span className={s.spacer} />
        {quote && (
          <>
            <span>{quote.name ?? normCode}</span>
            <span className={s.mono} style={{ color: toneColor(quote.change_pct) }}>
              {fmtPrice(quote.price)} {fmtPct(quote.change_pct)}
            </span>
            {quote.stale && <Badge tone="warning">过期数据</Badge>}
            {quote.source && <Badge tone="info">{quote.source}</Badge>}
          </>
        )}
      </div>

      <div className={s.split}>
        <Panel title="五档盘口">
          {!quote ? (
            <EmptyState text="未订阅到行情（WS 未连接或该标的不在推送范围）" />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 2, fontFamily: "var(--font-mono)" }}>
              {rows.map((r) => (
                <div
                  key={r.label}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "2px 4px",
                    background:
                      r.side === "mid"
                        ? "var(--bg-3)"
                        : r.side === "ask"
                          ? "var(--down-dim)"
                          : "var(--up-dim)",
                    borderRadius: "var(--radius-sm)",
                    position: "relative",
                    overflow: "hidden",
                  }}
                >
                  {/* 量能条：以该档位委托量占最大量的比例做背景填充 */}
                  {r.vol !== undefined && (
                    <div
                      style={{
                        position: "absolute",
                        left: 0,
                        top: 0,
                        bottom: 0,
                        width: `${(r.vol / maxVol) * 100}%`,
                        background: "var(--bg-hover)",
                        opacity: 0.5,
                      }}
                    />
                  )}
                  <span style={{ position: "relative", width: 36, color: "var(--text-dim)" }}>{r.label}</span>
                  <span
                    style={{
                      position: "relative",
                      flex: 1,
                      textAlign: "right",
                      color:
                        r.side === "ask"
                          ? "var(--down)"
                          : r.side === "bid"
                            ? "var(--up)"
                            : "var(--text)",
                      fontWeight: r.side === "mid" ? 600 : 400,
                    }}
                  >
                    {fmtPrice(r.price)}
                  </span>
                  <span style={{ position: "relative", width: 72, textAlign: "right", color: "var(--text-dim)" }}>
                    {r.vol === undefined ? "—" : fmtVolume(r.vol)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {quote && (
            <div className={s.kv} style={{ marginTop: 10 }}>
              <span className={s.kvKey}>今开</span>
              <span className={s.kvVal}>{fmtPrice(quote.open)}</span>
              <span className={s.kvKey}>最高</span>
              <span className={s.kvVal}>{fmtPrice(quote.high)}</span>
              <span className={s.kvKey}>最低</span>
              <span className={s.kvVal}>{fmtPrice(quote.low)}</span>
              <span className={s.kvKey}>昨收</span>
              <span className={s.kvVal}>{fmtPrice(quote.pre_close)}</span>
              <span className={s.kvKey}>总量</span>
              <span className={s.kvVal}>{fmtVolume(quote.volume)}</span>
            </div>
          )}
        </Panel>

        <Panel
          flush
          title={`逐笔成交（${ticks.length}）`}
          extra={
            <Button size="sm" variant="ghost" onClick={() => void l2.reload()}>
              刷新
            </Button>
          }
        >
          <div className={s.tableArea}>
            {l2.loading && !l2.data ? (
              <Spinner label="加载逐笔…" />
            ) : l2.error ? (
              <div className={`${s.note} ${s.noteWarn}`} style={{ margin: 8 }}>
                {l2.error}
                <div style={{ marginTop: 4 }}>
                  逐笔成交（L2）由券商网关提供，TDX 公共行情不包含逐笔明细。
                  未连接券商时该端点返回 503，这是零 mock 契约的预期行为。
                </div>
              </div>
            ) : ticks.length === 0 ? (
              <EmptyState text="暂无逐笔数据" />
            ) : (
              <div className={s.scroll}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--font-sm)" }}>
                  <thead>
                    <tr style={{ position: "sticky", top: 0, background: "var(--bg-2)" }}>
                      <th style={{ textAlign: "left", padding: "4px 8px", color: "var(--text-faint)" }}>时间</th>
                      <th style={{ textAlign: "right", padding: "4px 8px", color: "var(--text-faint)" }}>价格</th>
                      <th style={{ textAlign: "right", padding: "4px 8px", color: "var(--text-faint)" }}>数量</th>
                      <th style={{ textAlign: "center", padding: "4px 8px", color: "var(--text-faint)" }}>方向</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ticks.map((t, i) => (
                      <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td className={s.mono} style={{ padding: "2px 8px" }}>
                          {String(t.time ?? "--")}
                        </td>
                        <td className={s.mono} style={{ padding: "2px 8px", textAlign: "right" }}>
                          {fmtPrice(t.price)}
                        </td>
                        <td className={s.mono} style={{ padding: "2px 8px", textAlign: "right" }}>
                          {fmtVolume(t.volume)}
                        </td>
                        <td style={{ padding: "2px 8px", textAlign: "center" }}>
                          {t.side === undefined ? (
                            "—"
                          ) : (
                            <span style={{ color: t.side === "buy" ? "var(--up)" : "var(--down)" }}>
                              {t.side === "buy" ? "买" : t.side === "sell" ? "卖" : t.side}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

export default OrderBook;
