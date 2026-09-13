import { useMemo } from "react";
import { Badge, Button, EmptyState, Panel } from "@/design/primitives";
import { KLineChart } from "@/charts/KLineChart";
import { PERIOD_LABELS } from "@/shared/periods";
import { useQuotesStore } from "@/stores/quotes";
import { useWatchlistStore } from "@/stores/watchlist";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, fmtVolume, toneColor } from "@/shared/format";
import type { Period } from "@/shared/types";
import type { PageProps } from "@/app/routes";
import s from "./marketdata.module.css";

/**
 * K 线分析页（旗舰页）。
 *
 * 图表由 klinecharts 负责（专业画线/指标/联动）；
 * 五档盘口复用行情快照的 bid/ask 数组，不额外请求。
 */
export function MarketData({ params }: PageProps) {
  const code = (params.code as string) || "000001.SZ";
  const period = (params.period as Period) || "1d";
  const indicators = (params.indicators as string[]) ?? ["MA"];
  const linked = params.linked !== false;

  const codes = useMemo(() => [code], [code]);
  useQuoteSubscription(codes);

  const quote = useQuotesStore((st) => st.quotes[code]);
  const toggle = useWatchlistStore((st) => st.toggle);
  const inWatch = useWatchlistStore((st) => st.codes.includes(code));

  return (
    <div className={s.wrap}>
      <div className={s.header}>
        <span className={s.name}>{quote?.name ?? code}</span>
        <span className={s.code}>{code}</span>
        <span className={s.price} style={{ color: toneColor(quote?.change_pct) }}>
          {fmtPrice(quote?.price)}
        </span>
        <span style={{ color: toneColor(quote?.change_pct) }}>
          {fmtPrice(quote?.change)} {fmtPct(quote?.change_pct)}
        </span>
        <Badge tone="info">{PERIOD_LABELS[period]}</Badge>
        {quote?.source && <Badge tone={quote.stale ? "warning" : "neutral"}>{quote.source}</Badge>}
        {quote?.stale && <Badge tone="warning">数据可能滞后</Badge>}

        <span className={s.spacer} />
        <Button size="sm" variant={inWatch ? "default" : "primary"} onClick={() => toggle(code)}>
          {inWatch ? "移出自选" : "加自选"}
        </Button>
      </div>

      <div className={s.body}>
        <div className={s.chartArea}>
          <KLineChart
            code={code}
            period={period}
            indicators={indicators}
            linkGroupName={linked ? "default" : undefined}
          />
        </div>

        <div className={s.side}>
          <Panel title="五档盘口" flush>
            {quote?.bid || quote?.ask ? (
              <div className={s.book}>
                {(quote?.ask ?? [])
                  .slice(0, 5)
                  .reverse()
                  .map((p, i, arr) => {
                    const lvl = arr.length - i;
                    return (
                      <div key={`a${lvl}`} className={s.bookRow}>
                        <span className={s.bookLabel}>卖{lvl}</span>
                        <span className={s.bookPrice} style={{ color: "var(--down)" }}>
                          {fmtPrice(p)}
                        </span>
                        <span className={s.bookVol}>{fmtVolume(quote?.ask_vol?.[lvl - 1])}</span>
                      </div>
                    );
                  })}
                <div className={s.bookSplit} />
                {(quote?.bid ?? []).slice(0, 5).map((p, i) => (
                  <div key={`b${i + 1}`} className={s.bookRow}>
                    <span className={s.bookLabel}>买{i + 1}</span>
                    <span className={s.bookPrice} style={{ color: "var(--up)" }}>
                      {fmtPrice(p)}
                    </span>
                    <span className={s.bookVol}>{fmtVolume(quote?.bid_vol?.[i])}</span>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState text="暂无盘口数据（需券商推送五档）" />
            )}
          </Panel>

          <Panel title="关键数据" flush>
            <div className={s.stats}>
              {[
                ["今开", fmtPrice(quote?.open)],
                ["昨收", fmtPrice(quote?.pre_close)],
                ["最高", fmtPrice(quote?.high)],
                ["最低", fmtPrice(quote?.low)],
                ["成交量", fmtVolume(quote?.volume)],
                ["更新时间", quote?.time ?? "--"],
              ].map(([k, v]) => (
                <div key={k} className={s.statRow}>
                  <span className={s.statKey}>{k}</span>
                  <span className={s.statVal}>{v}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

export default MarketData;
