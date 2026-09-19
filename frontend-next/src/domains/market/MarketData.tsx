import { useMemo } from "react";
import { Badge, Button, Panel, TradingDateBadge } from "@/design/primitives";
import { KLineChart, DEFAULT_INDICATORS } from "@/charts/KLineChart";
import { PERIOD_LABELS } from "@/shared/periods";
import { useQuotesStore } from "@/stores/quotes";
import { useWatchlistStore } from "@/stores/watchlist";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, namePair, toneColor } from "@/shared/format";
import type { Period } from "@/shared/types";
import type { PageProps } from "@/app/routes";
import { OrderBookPanel } from "./panels/OrderBookPanel";
import s from "./marketdata.module.css";

/**
 * K 线分析页（旗舰页）。
 *
 * 图表由 klinecharts 负责（专业画线/指标/联动）；
 * 五档盘口复用行情快照的 bid/ask 数组，不额外请求。
 * 需要「报价牌 + K 线 + 分时 + 盘口 + 成交流」一起看时用「行情工作台」页。
 */
export function MarketData({ params }: PageProps) {
  const code = (params.code as string) || "000001.SZ";
  const period = (params.period as Period) || "1d";
  // ★ 必须 memo：`?? ["MA"]` 每次 render 都是新数组，而 KLineChart 的 effect
  // 依赖里含指标（V11 R14 已改为序列化 key，但这里仍不该每次新建引用）。
  const indicators = useMemo<readonly string[]>(
    () => (params.indicators as string[] | undefined) ?? DEFAULT_INDICATORS,
    [params.indicators],
  );
  const linked = params.linked !== false;

  const codes = useMemo(() => [code], [code]);
  useQuoteSubscription(codes);

  const quote = useQuotesStore((st) => st.quotes[code]);
  const toggle = useWatchlistStore((st) => st.toggle);
  const inWatch = useWatchlistStore((st) => st.codes.includes(code));
  // 名称未知时只显示一次代码（否则名称槽与代码槽会并排重复，同 DataPanel 的重影）
  const [title, sub] = namePair(quote?.name, code);

  return (
    <div className={s.wrap}>
      <div className={s.header}>
        <span className={s.name}>{title}</span>
        {sub && <span className={s.code}>{sub}</span>}
        <span className={s.price} style={{ color: toneColor(quote?.change_pct) }}>
          {fmtPrice(quote?.price)}
        </span>
        <span style={{ color: toneColor(quote?.change_pct) }}>
          {fmtPrice(quote?.change)} {fmtPct(quote?.change_pct)}
        </span>
        <Badge tone="info">{PERIOD_LABELS[period]}</Badge>
        {quote?.source && <Badge tone={quote.stale ? "warning" : "neutral"}>{quote.source}</Badge>}
        {quote?.stale && <Badge tone="warning">数据可能滞后</Badge>}
        {/* 「今日/最近交易日」——非交易日看行情时唯一能说明数据属于哪天的信息 */}
        <TradingDateBadge />

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
          {/* 盘口 + 关键数据统一由 OrderBookPanel 渲染（工作台右栏同一份实现）：
              原先此处另写一套「卖5→买1 / 买1→买5」，缺失值处理已与 OrderBook 页
              出现细微差异（一处 `--` 一处 `—`），合并展示要消灭的就是这种漂移。 */}
          <Panel title="五档盘口" flush>
            <OrderBookPanel code={code} />
          </Panel>
        </div>
      </div>
    </div>
  );
}

export default MarketData;
