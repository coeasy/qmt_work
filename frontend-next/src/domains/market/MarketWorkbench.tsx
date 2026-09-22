import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, Input, Panel, Tabs, TradingDateBadge } from "@/design/primitives";
import { KLineChart } from "@/charts/KLineChart";
import { IndicatorPicker } from "@/charts/IndicatorPicker";
import { DEFAULT_INDICATORS, DEFAULT_SUB_INDICATORS } from "@/charts/KLineChart";
import { PERIODS, PERIOD_LABELS } from "@/shared/periods";
import { useWatchlistStore } from "@/stores/watchlist";
import { useWorkspaceStore } from "@/stores/workspace";
import { useDisplayQuotes } from "@/hooks/useLiveQuotes";
import { fmtPct, fmtPrice, namePair, normalizeCode, toneColor } from "@/shared/format";
import type { Period } from "@/shared/types";
import type { PageProps } from "@/app/routes";
import { OrderForm } from "@/domains/trading/OrderForm";
import { MinutesChart } from "./panels/MinutesChart";
import { OrderBookPanel } from "./panels/OrderBookPanel";
import { L2Panel } from "./panels/L2Panel";
import { DealFeedPanel } from "./panels/DealFeedPanel";
import { StockInfoPanel } from "./panels/StockInfoPanel";
import { FundamentalsPanel } from "./panels/FundamentalsPanel";
import s from "./panels/panels.module.css";
import d from "../domain.module.css";

/**
 * 行情工作台 —— 把「报价牌 / K 线分析 / 分时图 / 盘口逐笔 / 成交明细 / 股票基本信息 /
 * 基本面信息 / 手动下单」合并到一个界面（对标主流 A 股终端的「看盘 + 交易」同屏）。
 *
 * 为什么合并：这些本来就是看一只票的同一件事，拆成多个独立 Tab 的代价是
 * ① 每次换标的要在每个 Tab 里各改一次代码（改漏一个就看到两只票的数据混在一起）；
 * ② 报价牌与盘口/成交流互相看不见，看盘时要在 Tab 之间来回切；
 * ③ 看到机会要下单还得跳页重填代码。
 * 工作台把「标的」提升为**页面级状态**：换标的时中栏图表、右栏盘口/成交流、
 * 底部资料与下单面板**同时切换**，不可能再出现多处标的不一致。
 *
 * 布局（两列 + 中列底部坞 —— 见 panels.module.css 的 1180px / 900px 断点）：
 *   中：上 = K 线 / 分时（Tab 切换，K 线带周期切换条）
 *       下 = 底部坞：基本面 / 完整下单（可折叠，默认停在基本面）
 *   右：五档盘口 + 基本信息（可折叠，默认展开）+ 逐笔 / 成交流（Tab 切换）+ 快捷下单
 *
 * ★★ 本页**不再内嵌自选股列表**。自选股的唯一常驻位置是**左侧数据面板**
 *   （`shell/DataPanel`，默认展开且默认就是「自选股」页签，全局任何页面都能看到）。
 *   此前右栏也放了一份，结果是同一份自选股在屏幕上出现两次：
 *   ① 占掉右栏近 200px 竖向空间，把成交流与下单往下顶；
 *   ② 两处各自维护「当前标的」的视觉状态，改一处另一处不跟随 ⇒ 看起来像两个不同的列表。
 *   现在右栏只留「这一只票」的信息（五档 / 成交流 / 下单），自选股交给左侧。
 *
 * ★ 底部坞为什么不放在右列：右列只有 260–320px，塞不下第 5 个页签；中列宽度充裕，
 *   而且「图表在上、下单在下」正是交易时的视线动线。
 *
 * ★ 独立页保留：头部「独立打开」按钮组把当前标的带到对应独立页 —— 合并展示不等于
 *   取消独立页（多显示器 / 分栏对照时独立页仍然有用）。这些独立页已从主菜单移除
 *   （`routes.tsx` 的 `menu: false`），只能从这里进，避免菜单里出现重复入口。
 *
 * 零 mock：所有数字都来自 WS 快照或后端接口；无数据时各面板显式说明原因。
 */

type View = "kline" | "minutes";
type Flow = "l2" | "deals";
/**
 * 底部坞页签。
 *
 * ★ 2026-09-21：「基本信息」**从这里移到了右栏**（见下方右栏注释）——
 *   那时候它同时出现在底部坞和右栏两处，跟「自选股左右各一份」是同一类重复。
 *   底部坞只留「基本面（财务）」与「完整下单」，折叠按钮照旧把高度还给图表。
 */
type Dock = "fundamentals" | "trade";

/**
 * ★★ 工作台默认标的 = **A 股大盘（上证指数）**。
 *
 * ⚠️⚠️ 这个常量是全站最容易写错的一处：
 *   - `000001.SH` → **上证指数**（A 股大盘，本常量要的就是它）
 *   - `000001.SZ` → **平安银行**（个股！数字完全相同，只差后缀）
 * 写错的后果极隐蔽：界面照常出图，只是默认看的是一只银行股而不是大盘 ——
 * 不报错、不看代码根本发现不了。因此这里**必须带后缀写全**并留此注释。
 *
 * 为什么默认大盘而不是自选股第一只：工作台是「打开就看盘」的页，
 * ① 大盘是所有人的共同语境（先知道今天什么行情，再看个股）；
 * ② 自选股可能为空，那时只能回退到某个个股，语义上是错的。
 */
const DEFAULT_WORKBENCH_CODE = "000001.SH";

export function MarketWorkbench({ params, tabId }: PageProps) {
  const initialCode = useMemo(() => {
    const fromParams = (params.code as string) || "";
    if (fromParams) return normalizeCode(fromParams);
    // 未指定标的时看 A 股大盘（见 DEFAULT_WORKBENCH_CODE 的警示）
    return DEFAULT_WORKBENCH_CODE;
  }, [params.code]);

  const [code, setCode] = useState(initialCode);
  const [draft, setDraft] = useState(initialCode);
  const [period, setPeriod] = useState<Period>((params.period as Period) || "1d");
  const [view, setView] = useState<View>((params.view as View) || "kline");
  const [flow, setFlow] = useState<Flow>("deals");
  /** 底部坞当前页签；null = 已折叠（把高度还给图表） */
  const [dock, setDock] = useState<Dock | null>("fundamentals");
  /** 右栏「基本信息」是否展开（默认展开：它就是给用户「快速查看」的） */
  const [infoOpen, setInfoOpen] = useState(true);
  /** K 线叠加指标（主图 / 副图） */
  const [mainInd, setMainInd] = useState<string[]>([...DEFAULT_INDICATORS]);
  const [subInd, setSubInd] = useState<string[]>([...DEFAULT_SUB_INDICATORS]);
  const [indNotice, setIndNotice] = useState("");

  const open = useWorkspaceStore((st) => st.open);
  const renameTab = useWorkspaceStore((st) => st.renameTab);
  const toggle = useWatchlistStore((st) => st.toggle);
  const inWatch = useWatchlistStore((st) => st.codes.includes(code));

  /**
   * ★ 走 `useDisplayQuotes` 而不是 `useQuotesStore`：后者只认 WS 推送，
   * **休市 / 盘后 / 没连券商时一条都不推** ⇒ 工作台头部价格恒为 `--`。
   * 兜底层会拉最近交易日收盘，于是「周六打开工作台」也能看到数字（
   * 顶部 `TradingDateBadge` 同时标明这是哪一天的数据）。
   * `useDisplayQuotes` 内部已含订阅，无需再调 `useQuoteSubscription`。
   */
  const quote = useDisplayQuotes(useMemo(() => [code], [code]))[code];

  /**
   * Tab 标题跟着解析出来的真名走。
   *
   * 为什么必须在这里补：标题在 `open()` 那一刻定死，而点进来时名称常常还没到位
   * （WS 未推 / REST 兜底未回）⇒ 标题永久停在 `000001.SZ`。
   * 名称到位后由页面主动 `renameTab`，切标的时也会跟着变。
   */
  const resolvedName = String(quote?.name ?? "").trim();
  useEffect(() => {
    if (tabId && resolvedName) renameTab(tabId, resolvedName);
  }, [tabId, resolvedName, renameTab]);

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

  const dockTitle = dock === "fundamentals" ? "基本面" : "完整下单";
  return (
    <div className={s.work}>
      {/* ---------- 主区：K 线 / 分时 + 底部坞 ----------
          ★ 这一列不再有报价牌：整块宽度都归图表（K 线能多显示约三分之一的
            横向区间）。自选股在**左侧数据面板**，点一只即开/切到它的工作台。 */}
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
            {/* 这些独立页已从主菜单移除（routes.tsx: menu:false），入口只在这里 */}
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
              {/* 动态指标：勾选即叠加/移除，不重建图表（见 KLineChart 的指标 effect） */}
              <IndicatorPicker
                main={mainInd}
                sub={subInd}
                onChange={(m, sb) => {
                  setMainInd(m);
                  setSubInd(sb);
                  setIndNotice("");
                }}
                onNotice={setIndNotice}
              />
              {indNotice && <div className={s.panelNote}>{indNotice}</div>}
              <div className={s.chartArea}>
                {/* 头部已显示名称/价格，图表内不再重复一份 readout */}
                <KLineChart
                  code={code}
                  period={period}
                  showReadout={false}
                  indicators={mainInd}
                  subIndicators={subInd}
                />
              </div>
            </>
          ) : (
            <MinutesChart code={code} />
          )}
        </Panel>

        {/* ---------- 中列底部坞：基本信息 / 基本面 / 交易 ---------- */}
        <Panel
          flush
          className={[s.dock, dock === null ? s.dockCollapsed : ""].filter(Boolean).join(" ")}
          bodyClassName={s.dockBody}
          title={dock === null ? "资料与下单（已折叠）" : dockTitle}
          extra={
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              {dock !== null && (
                <Tabs
                  items={[
                    { key: "fundamentals", label: "基本面" },
                    { key: "trade", label: "完整下单" },
                  ]}
                  value={dock}
                  onChange={(k) => setDock(k as Dock)}
                />
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setDock(dock === null ? "fundamentals" : null)}
                title={dock === null ? "展开基本面与下单" : "折叠（把高度还给图表）"}
              >
                {dock === null ? "展开" : "折叠"}
              </Button>
            </div>
          }
        >
          {dock === "fundamentals" && <FundamentalsPanel code={code} />}
          {dock === "trade" && <OrderForm code={code} compact onCodeChange={pick} />}
        </Panel>
      </div>

      {/* ---------- 右：五档 → 基本信息 → 逐笔/成交流 → 快捷下单 ----------
          顺序按「看盘动线」排：先看到能成交的价格（五档），再快速扫一眼这只票
          是什么（基本信息），然后看资金在怎么动（成交流），最后**手不用离开
          这一侧**就能下单。
          ★ 这一列不再放自选股（原在五档与成交流之间）：自选股在左侧数据面板，
            这里再放一份就是同一份数据两处展示。
          ★ 「基本信息」2026-09-21 从底部坞挪到这里：底部坞要展开才看得见，
            而「这只票市值多少、PE 多少、涨跌停在哪」是看盘时**随时要瞄一眼**的，
            放在右栏常驻才能「快速查看」。底部坞因此只剩基本面与完整下单。 */}
      <div className={s.workRight}>
        <Panel title="五档盘口" flush>
          <OrderBookPanel code={code} />
        </Panel>

        <Panel
          flush
          title="基本信息"
          extra={
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setInfoOpen((v) => !v)}
              title={infoOpen ? "折叠（把高度还给成交流）" : "展开基本信息"}
            >
              {infoOpen ? "折叠" : "展开"}
            </Button>
          }
        >
          {infoOpen ? (
            // 限高 + 内部滚动：右栏还要留给成交流与快捷下单，不能被这块撑爆
            <StockInfoPanel code={code} maxBodyHeight={210} />
          ) : (
            // ★ 折叠态也要说明「这里有什么、怎么打开」，而不是留一块白板
            <div className={s.shrunkRow}>
              <span>已折叠 —— 点「展开」查看 {code} 的基本信息</span>
            </div>
          )}
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

        {/* 右下角快捷下单：标的跟随工作台当前标的，改标的不用重填 */}
        <Panel title="快捷下单" flush>
          <OrderForm code={code} quick onCodeChange={pick} />
        </Panel>
      </div>
    </div>
  );
}

export default MarketWorkbench;
