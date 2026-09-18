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
 * ★ 契约要点：
 *   - 五档来自 /market/quote 的 bid/ask 与 bid_vol/ask_vol 数组（长度可能不足 5，按实际渲染）
 *   - 逐笔走 /market/l2（券商 get_l2_transactions），**券商未连接时返回 503**；
 *     TDX 公共行情不提供逐笔，故本页在无券商时逐笔区域明确提示而非空白
 *   - 行情快照本身由 WS 订阅驱动（useQuoteSubscription），无需轮询
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
