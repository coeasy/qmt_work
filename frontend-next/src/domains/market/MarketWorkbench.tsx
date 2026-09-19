import { useCallback, useMemo, useState } from "react";
import { Badge, Button, Input, Panel, Tabs, TradingDateBadge } from "@/design/primitives";
import { KLineChart } from "@/charts/KLineChart";
import { PERIODS, PERIOD_LABELS } from "@/shared/periods";
import { useQuotesStore } from "@/stores/quotes";
import { useWatchlistStore } from "@/stores/watchlist";
import { useWorkspaceStore } from "@/stores/workspace";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, namePair, normalizeCode, toneColor } from "@/shared/format";
import type { Period } from "@/shared/types";
import type { PageProps } from "@/app/routes";
import { QuoteBoard } from "./QuoteBoard";
import { MinutesChart } from "./panels/MinutesChart";
import { OrderBookPanel } from "./panels/OrderBookPanel";
import { L2Panel } from "./panels/L2Panel";
import { DealFeedPanel } from "./panels/DealFeedPanel";
import s from "./panels/panels.module.css";
import d from "../domain.module.css";

/**
 * 行情工作台 —— 把「报价牌 / K 线分析 / 分时图 / 盘口逐笔 / 成交明细」合并到一个界面。
 *
 * 为什么合并：这 5 项本来就是看一只票的同一件事，拆成 5 个独立 Tab 的代价是
 * ① 每次换标的要在 5 个 Tab 里各改一次代码（改漏一个就看到两只票的数据混在一起）；
 * ② 报价牌与盘口/成交流互相看不见，看盘时要在 Tab 之间来回切。
 * 工作台把「标的」提升为**页面级状态**：左栏点一下，中栏图表与右栏盘口/成交流
 * 同时切换，不可能再出现三处标的不一致。
 *
 * 布局（三列，窄屏收起左栏 —— 见 panels.module.css 的 1180px 断点）：
 *   左：报价牌（自选股，点行切换标的，当前标的高亮）
 *   中：K 线 / 分时（Tab 切换，K 线带周期切换条）
 *   右：五档盘口 + 逐笔 / 成交流（Tab 切换）
 *
 * 保留独立入口：头部「独立打开」按钮组把当前标的带到对应独立页 ——
 * 合并展示不等于取消独立页（多显示器 / 分栏对照时独立页仍然有用）。
 *
 * 零 mock：所有数字都来自 WS 快照或后端接口；无数据时各面板显式说明原因。
 */

type View = "kline" | "minutes";
type Flow = "l2" | "deals";

export function MarketWorkbench({ params }: PageProps) {
  const initialCode = useMemo(() => {
    const fromParams = (params.code as string) || "";
    if (fromParams) return normalizeCode(fromParams);
    // 未指定标的时取自选股第一只：工作台是「看盘」页，空着比给个默认票更难受
    return useWatchlistStore.getState().codes[0] ?? "000001.SZ";
  }, [params.code]);

  const [code, setCode] = useState(initialCode);
  const [draft, setDraft] = useState(initialCode);
  const [period, setPeriod] = useState<Period>((params.period as Period) || "1d");
  const [view, setView] = useState<View>((params.view as View) || "kline");
  const [flow, setFlow] = useState<Flow>("deals");

  const open = useWorkspaceStore((st) => st.open);
  const quote = useQuotesStore((st) => st.quotes[code]);
  const toggle = useWatchlistStore((st) => st.toggle);
  const inWatch = useWatchlistStore((st) => st.codes.includes(code));

  useQuoteSubscription(useMemo(() => [code], [code]));

  // 名称未知时只显示代码一次（名称槽与代码槽并排重复会成「重影」）
  const [title, sub] = namePair(quote?.name, code);

  const pick = useCallback((next: string) => {
    const c = normalizeCode(next);
    if (!c) return;
    setCode(c);
    setDraft(c);
  }, []);

  const commitDraft = useCallback(() => {
    const raw = draft.trim();
    if (!raw) return;
    // 支持「600519」与「600519.SH」两种输入；裸 6 位由 normalizeCode 补市场后缀
    if (/^\d{6}(\.[A-Za-z]{2})?$/.test(raw)) pick(raw);
  }, [draft, pick]);

  return (
    <div className={s.work}>
      {/* ---------- 左：报价牌 ---------- */}
      <div className={s.workAside}>
        <QuoteBoard compact active={code} onPick={pick} />
      </div>

      {/* ---------- 中：K 线 / 分时 ---------- */}
      <div className={s.workMain}>
        <div className={s.workHeader}>
          <span className={s.symbolName}>{title}</span>
          {sub && <span className={s.symbolCode}>{sub}</span>}
          <span className={s.symbolPrice} style={{ color: toneColor(quote?.change_pct) }}>
            {fmtPrice(quote?.price)}
          </span>
          <span style={{ color: toneColor(quote?.change_pct) }}>
            {fmtPrice(quote?.change)} {fmtPct(quote?.change_pct)}
          </span>
          {quote?.source && (
            <Badge tone={quote.stale ? "warning" : "neutral"}>{quote.source}</Badge>
          )}
          {quote?.stale && <Badge tone="warning">数据可能滞后</Badge>}
          {!quote && <Badge tone="warning">未订阅到行情</Badge>}
          {/* 非交易日必须显式说明「下面是最近交易日的数据」，否则周六看到的
              数字会被当成今日行情（后端返回上一交易日数据本身是正确的）。 */}
          <TradingDateBadge />

          <span className={d.spacer} />

          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitDraft();
            }}
            mono
            style={{ width: 108 }}
            placeholder="代码"
          />
          <Button size="sm" variant={inWatch ? "default" : "primary"} onClick={() => toggle(code)}>
            {inWatch ? "移出自选" : "加自选"}
          </Button>
          <span className={s.linkBar}>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                open("quote", { code, period }, { title: `${title} · K 线`, reuse: "new" })
              }
            >
              独立 K 线
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => open("minutes", { code }, { title: `${title} · 分时`, reuse: "new" })}
            >
              独立分时
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                open("orderbook", { code }, { title: `${title} · 盘口逐笔`, reuse: "new" })
              }
            >
              独立盘口
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                open("deal_feed", { code }, { title: `${title} · 成交明细`, reuse: "new" })
              }
            >
              独立成交
            </Button>
          </span>
        </div>

        <Panel
          flush
          className={s.grow}
          bodyClassName={s.bodyCol}
          title={view === "kline" ? `K 线分析 · ${PERIOD_LABELS[period]}` : "分时图"}
          extra={
            <Tabs
              items={[
                { key: "kline", label: "K 线" },
                { key: "minutes", label: "分时" },
              ]}
              value={view}
              onChange={(k) => setView(k as View)}
            />
          }
        >
          {view === "kline" ? (
            <>
              <div className={s.periodBar}>
                {PERIODS.map((p) => (
                  <button
                    key={p}
                    type="button"
                    className={[s.periodBtn, p === period ? s.periodBtnActive : ""]
                      .filter(Boolean)
                      .join(" ")}
                    onClick={() => setPeriod(p)}
                  >
                    {PERIOD_LABELS[p]}
                  </button>
                ))}
              </div>
              <div className={s.chartArea}>
                {/* 头部已显示名称/价格，图表内不再重复一份 readout */}
                <KLineChart code={code} period={period} showReadout={false} />
              </div>
            </>
          ) : (
            <MinutesChart code={code} />
          )}
        </Panel>
      </div>

      {/* ---------- 右：盘口 + 逐笔 / 成交流 ---------- */}
      <div className={s.workRight}>
        <Panel title="五档盘口" flush>
          <OrderBookPanel code={code} />
        </Panel>

        <Panel
          flush
          className={s.grow}
          bodyClassName={s.bodyCol}
          title={flow === "l2" ? "逐笔成交（券商 L2）" : "实时成交流"}
          extra={
            <Tabs
              items={[
                { key: "l2", label: "逐笔" },
                { key: "deals", label: "成交流" },
              ]}
              value={flow}
              onChange={(k) => setFlow(k as Flow)}
            />
          }
        >
          {flow === "l2" ? (
            <L2Panel code={code} />
          ) : (
            // 工作台头部已标明标的，成交流不必每行重复代码列
            <DealFeedPanel code={code} showCode={false} />
          )}
        </Panel>
      </div>
    </div>
  );
}

export default MarketWorkbench;
