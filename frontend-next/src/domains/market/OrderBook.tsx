import { useMemo, useState } from "react";
import { Badge, Button, Input, Panel } from "@/design/primitives";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, namePair, normalizeCode, toneColor } from "@/shared/format";
import type { PageProps } from "@/app/routes";
import { OrderBookPanel } from "./panels/OrderBookPanel";
import { L2Panel } from "./panels/L2Panel";
import s from "../domain.module.css";

/**
 * 盘口 / 逐笔成交（独立页）。
 *
 * 两块内容都在 `panels/` 下（行情工作台右栏共用同一份实现）——
 * 本页只负责「输入代码 / 逐笔条数 + 刷新」这层外壳。
 *
 * ★ 契约要点（2026-10-03 修订，与 `panels/OrderBookPanel.tsx` 同口径）：
 *   - 五档来自 /market/quote 的 **`bids` / `asks`** 数组（元素 `{price, volume}`）。
 *     ⚠️ 旧注释写的「bid/ask 与 bid_vol/ask_vol 数组」是**错的**：那四个是买一/卖一
 *     **标量**（`xtquant_client/xtp/quotes.py` 的 `_lst(..., 0)`），按数组索引读恒为
 *     undefined ⇒ 五档整列「—」。以 `bids` / `asks` 为准。
 *   - 取值走「WS 推送优先 + `GET /market/quote` 兜底」：未连券商时 WS 一条不推，
 *     而本地行情源同样给得出五档，故盘口**不应**在无券商时恒空（面板内 5s 轮询）。
 *   - 逐笔（L2）走 /market/l2（券商 `get_l2_transactions`），**券商未连接时返回 503**；
 *     无需券商的市场逐笔另走 `/market/ticks`（见 `MarketTicksPanel` / 独立「逐笔成交」页）。
 *
 * 零 mock：任一档位/逐笔缺失时显示「—」，不用 0 填充。
 * 支持 `params.code` 预填（工作台「独立盘口」按钮带入当前标的）。
 */
export function OrderBook({ params }: PageProps) {
  const [code, setCode] = useState((params.code as string) || "000001.SZ");
  const [l2Count, setL2Count] = useState("50");
  const [token, setToken] = useState(0);

  const normCode = useMemo(() => normalizeCode(code), [code]);
  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);
  const [title, sub] = namePair(quote?.name, normCode);
  const count = Number(l2Count) || 50;

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          mono
          style={{ width: 150 }}
          placeholder="代码"
        />
        <Input
          value={l2Count}
          onChange={(e) => setL2Count(e.target.value)}
          mono
          style={{ width: 70 }}
          title="逐笔条数"
        />
        <Button size="sm" variant="ghost" onClick={() => setToken((t) => t + 1)}>
          刷新逐笔
        </Button>
        <span className={s.spacer} />
        <span>{title}</span>
        {sub && <span className={s.muted}>{sub}</span>}
        {quote && (
          <>
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
          <OrderBookPanel code={normCode} />
        </Panel>

        <Panel
          flush
          title="逐笔成交（券商 L2）"
          extra={
            <Button size="sm" variant="ghost" onClick={() => setToken((t) => t + 1)}>
              刷新
            </Button>
          }
        >
          <div className={s.tableArea}>
            <L2Panel code={normCode} count={count} reloadToken={token} />
          </div>
        </Panel>
      </div>
    </div>
  );
}

export default OrderBook;
