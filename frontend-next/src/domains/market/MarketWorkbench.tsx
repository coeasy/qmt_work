import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, Input, Panel, Tabs, TradingDateBadge } from "@/design/primitives";
import { KLineChart } from "@/charts/KLineChart";
import { IndicatorPicker } from "@/charts/IndicatorPicker";
import { DEFAULT_INDICATORS, DEFAULT_SUB_INDICATORS } from "@/charts/KLineChart";
import { PERIODS, PERIOD_LABELS } from "@/shared/periods";
import { useWatchlistStore } from "@/stores/watchlist";
import { useWorkspaceStore } from "@/stores/workspace";
import { useDisplayQuotes } from "@/hooks/useLiveQuotes";
import { fmtPct, fmtPrice, fmtSigned, namePair, normalizeCode, toneColor } from "@/shared/format";
import type { Instrument, Period } from "@/shared/types";
import { marketApi } from "@/services/api";
import type { PageProps } from "@/app/routes";
import { OrderForm } from "@/domains/trading/OrderForm";
import { MinutesChart } from "./panels/MinutesChart";
import { OrderBookPanel } from "./panels/OrderBookPanel";
import { MarketTicksPanel } from "./panels/MarketTicksPanel";
import { StockInfoPanel } from "./panels/StockInfoPanel";
import { FundamentalsPanel } from "./panels/FundamentalsPanel";
import s from "./panels/panels.module.css";
import d from "../domain.module.css";

/**
 * 行情工作台 —— 把「报价牌 / K 线分析 / 分时图 / 盘口五档 / 逐笔成交 / 股票基本信息 /
 * 基本面信息 / 手动下单」合并到一个界面（对标主流 A 股终端的「看盘 + 交易」同屏）。
 *
 * 为什么合并：这些本来就是看一只票的同一件事，拆成多个独立 Tab 的代价是
 * ① 每次换标的要在每个 Tab 里各改一次代码（改漏一个就看到两只票的数据混在一起）；
 * ② 报价牌与盘口/成交流互相看不见，看盘时要在 Tab 之间来回切；
 * ③ 看到机会要下单还得跳页重填代码。
 * 工作台把「标的」提升为**页面级状态**：换标的时中栏图表、右栏盘口/成交流、
 * 底部资料与下单面板**同时切换**，不可能再出现多处标的不一致。
 *
 * 布局（两列 —— 见 panels.module.css 的 1180px / 900px 断点）：
 *   中：头部工具条 + K 线 / 分时（Tab 切换，K 线带周期切换条）**吃满整列高度**
 *   右：① 资料（「基本信息 | 基本面」Tab）
 *       ② 盘口与成交（「五档盘口 | 成交流」Tab，占满剩余高度）
 *       ③ 下单
 *
 * ★ 2026-10-03 第二轮布局重构（为什么又改）：
 *   ① **去掉中列底部坞**。它的存在前提是「基本面只能放在中列」——而基本面已经
 *      与基本信息合成一块「资料」放在右栏，底部坞便只剩下单，等于用 42% 的图表
 *      高度换一块重复的下单区。去掉后图表吃满整列高度（看盘页的主角是图）。
 *   ② **右栏改成「按用途分三块」而不是「按数据源堆 5 块」**：资料 / 盘口与成交 /
 *      下单。原先「五档盘口」「基本信息」「逐笔·成交流」「快捷下单」四块平铺，
 *      每块都有自己的标题栏与内边距，300px 宽的栏里光标题就吃掉可观高度。
 *      合并成两块 Tab 后，同样的高度能多显示 5~6 行数据。
 *   ③ **成交流的真源换成市场逐笔**（`/market/ticks`，本地 TDX）。原先成交流读的
 *      是 WS 的 `deal` 事件，那是**本账户成交回报**（见 `MarketTicksPanel` 注释），
 *      未连券商 / 看别人的票时恒空，且标题叫「实时成交流」会被误读成市场成交。
 *      换源后无需券商即有真实逐笔。
 *   ④ 「逐笔(L2)」（券商 L2）从右栏移出 —— 它是**券商专属**能力，且与市场逐笔
 *      信息高度重叠；在 300px 宽的栏里再占一个 Tab 得不偿失。它仍可从头部
 *      「独立盘口」进入（`OrderBook.tsx` 同时渲染盘口与 L2）。
 *
 * ★★ 本页**不再内嵌自选股列表**。自选股的唯一常驻位置是**左侧数据面板**
 *   （`shell/DataPanel`，默认展开且默认就是「自选股」页签，全局任何页面都能看到）。
 *   此前右栏也放了一份，结果是同一份自选股在屏幕上出现两次：
 *   ① 占掉右栏近 200px 竖向空间，把成交流与下单往下顶；
 *   ② 两处各自维护「当前标的」的视觉状态，改一处另一处不跟随 ⇒ 看起来像两个不同的列表。
 *   现在右栏只留「这一只票」的信息（五档 / 成交流 / 下单），自选股交给左侧。
 *
 * ★ 独立页保留：头部「独立打开」按钮组把当前标的带到对应独立页 —— 合并展示不等于
 *   取消独立页（多显示器 / 分栏对照时独立页仍然有用）。这些独立页已从主菜单移除
 *   （`routes.tsx` 的 `menu: false`），只能从这里进，避免菜单里出现重复入口。
 *
 * 零 mock：所有数字都来自 WS 快照或后端接口；无数据时各面板显式说明原因。
 */

type View = "kline" | "minutes";
/** 右栏「盘口与成交」页签：五档盘口 / 成交流（真实市场逐笔，无需券商） */
type Flow = "book" | "ticks";
/** 右栏「资料」页签：基本信息 / 基本面（财务·估值·资金流） */
type InfoTab = "info" | "fundamentals";

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
  /** 右栏「盘口与成交」当前页签；默认成交流（看资金动向比只盯五档信息量更大） */
  const [flow, setFlow] = useState<Flow>("ticks");
  /**
   * 「按名称 / 拼音找标的」的结果。
   *
   * ★ 为什么必须有这一层：输入框此前只认 `6 位数字[.SH]`（`commitDraft` 的正则），
   *   输入「茅台」「gzmt」时**回车什么都不会发生** —— 不报错、不提示，用户只会以为
   *   软件卡了。而后端 `GET /market/resolve` 本来就能把任意输入归一成标准代码 +
   *   候选，只是前端**没有调用点**（死声明，属孤儿逻辑）。这里把它接上。
   */
  const [resolveNote, setResolveNote] = useState<{
    q: string;
    cands: Instrument[];
    err?: string;
  } | null>(null);
  /** 右栏「资料」当前页签；默认基本信息（快速扫一眼市值/PE/涨跌停） */
  const [infoTab, setInfoTab] = useState<InfoTab>("info");
  /** 右栏「资料」是否展开（默认展开：它就是给用户「快速查看」的） */
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

  /**
   * 提交输入框：先按代码解析，不像代码就走 `/market/resolve`（名称 / 拼音 / 前缀式）。
   *
   * ⚠️ 唯一命中才直接切换；多候选时**必须**让用户选 —— 自动取第一个会在
   *   「中国」这类模糊输入上切到完全不相干的票，而界面上看起来像是「搜出来了」。
   */
  const commitDraft = useCallback(() => {
    const raw = draft.trim();
    if (!raw) return;
    if (/^\d{6}(\.[A-Za-z]{2})?$/.test(raw)) {
      pick(raw);
      setResolveNote(null);
      return;
    }
    marketApi
      .resolve(raw, 8)
      .then((r) => {
        const cands = Array.isArray(r?.candidates) ? r.candidates : [];
        if (r?.resolved && r.code) {
          pick(r.code);
          setResolveNote(null);
          return;
        }
        if (cands.length === 1 && cands[0]?.code) {
          pick(cands[0].code);
          setResolveNote(null);
          return;
        }
        setResolveNote({ q: raw, cands: cands.slice(0, 8) });
      })
      .catch((e: unknown) => {
        setResolveNote({
          q: raw,
          cands: [],
          err: e instanceof Error ? e.message : String(e),
        });
      });
  }, [draft, pick]);

  return (
    <div className={s.work}>
      {/* ---------- 主区：头部工具条 + K 线 / 分时（吃满整列高度）----------
          ★ 这一列不再有报价牌、也不再有底部坞：
            - 报价牌整块宽度让给图表（K 线能多显示约三分之一的横向区间）；
            - 底部坞去掉后图表直接吃满整列高度（看盘页的主角是图）；
            - 自选股在**左侧数据面板**，点一只即开/切到它的工作台。 */}
      <div className={s.workMain}>
        <div className={s.workHeader}>
          <span className={s.symbolName}>{title}</span>
          {sub && <span className={s.symbolCode}>{sub}</span>}
          <span className={s.symbolPrice} style={{ color: toneColor(quote?.change_pct) }}>
            {fmtPrice(quote?.price)}
          </span>
          <span style={{ color: toneColor(quote?.change_pct) }}>
            {fmtSigned(quote?.change)} {fmtPct(quote?.change_pct)}
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
            placeholder="代码 / 名称 / 拼音"
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
                open("deal_feed", { code }, { title: `${title} · 逐笔成交`, reuse: "new" })
              }
            >
              独立逐笔
            </Button>
          </span>
        </div>

        {/* 名称 / 拼音检索的结果条 —— 多候选必须让用户选（见 commitDraft 的说明）。
            没有这一条时，「茅台」回车后界面毫无反应（旧的静默失败）。 */}
        {resolveNote && (
          <div className={s.panelNote}>
            {resolveNote.err
              ? `标的解析失败：${resolveNote.err}`
              : resolveNote.cands.length
                ? `「${resolveNote.q}」匹配到 ${resolveNote.cands.length} 个候选，点选切换：`
                : `未找到与「${resolveNote.q}」匹配的标的`}
            <span className={s.linkBar} style={{ marginLeft: 6 }}>
              {resolveNote.cands.map((c) => (
                <button
                  key={c.code}
                  type="button"
                  className={s.periodBtn}
                  onClick={() => {
                    pick(c.code);
                    setResolveNote(null);
                  }}
                  title={c.code}
                >
                  {c.name || c.code}
                </button>
              ))}
            </span>
          </div>
        )}

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
      </div>

      {/* ---------- 右：资料 → 盘口与成交 → 下单 ----------
          顺序按「看盘动线」排，从「是什么」到「在怎么走」再到「下单」：
            ① 资料：这只票是什么（基本信息 / 基本面）；
            ② 盘口与成交：现在能成交的价格 + 资金在怎么动（五档 / 成交流）；
            ③ 下单：手不用离开这一侧。
          ★ 合并成两块 Tab 而不是平铺四块：300px 宽的栏里，每块 Panel 的标题栏
            与内边距都是固定开销；合并后同样的高度能多显示 5~6 行数据。
          ★ 这一列不再放自选股（原在五档与成交流之间）：自选股在左侧数据面板，
            这里再放一份就是同一份数据两处展示。
          ★ 「逐笔(L2)」也移出本栏：它是**券商专属**能力（未连接返 503），且与
            市场逐笔高度重叠；仍可从头部「独立盘口」进入。 */}
      <div className={s.workRight}>
        {/* ① 资料：基本信息 | 基本面 —— 合并成一块，点 Tab 切换 */}
        <Panel
          flush
          title="资料"
          extra={
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              {infoOpen && (
                <Tabs
                  items={[
                    { key: "info", label: "基本信息" },
                    { key: "fundamentals", label: "基本面" },
                  ]}
                  value={infoTab}
                  onChange={(k) => setInfoTab(k as InfoTab)}
                />
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setInfoOpen((v) => !v)}
                title={infoOpen ? "折叠（把高度还给盘口与成交流）" : "展开资料"}
              >
                {infoOpen ? "折叠" : "展开"}
              </Button>
            </div>
          }
        >
          {infoOpen ? (
            infoTab === "info" ? (
              // 限高 + 内部滚动：右栏还要留给盘口/成交流与下单，不能被这块撑爆
              <StockInfoPanel code={code} maxBodyHeight={210} />
            ) : (
              <div className={s.infoBody}>
                <FundamentalsPanel code={code} />
              </div>
            )
          ) : (
            // ★ 折叠态也要说明「这里有什么、怎么打开」，而不是留一块白板
            <div className={s.shrunkRow}>
              <span>已折叠 —— 点「展开」查看 {code} 的基本信息 / 基本面</span>
            </div>
          )}
        </Panel>

        {/* ② 盘口与成交：五档盘口 | 成交流（真实市场逐笔，无需券商）*/}
        <Panel
          flush
          className={[s.grow, s.flowPanel].join(" ")}
          bodyClassName={s.bodyCol}
          title="盘口与成交"
          extra={
            <Tabs
              items={[
                { key: "ticks", label: "成交流" },
                { key: "book", label: "五档盘口" },
              ]}
              value={flow}
              onChange={(k) => setFlow(k as Flow)}
            />
          }
        >
          {flow === "book" ? (
            <div className={s.bookBody}>
              <OrderBookPanel code={code} />
            </div>
          ) : (
            // 工作台头部已标明标的，成交流不必每行重复代码列
            <MarketTicksPanel code={code} />
          )}
        </Panel>

        {/* ③ 下单：标的跟随工作台当前标的，改标的不用重填 */}
        <Panel title="下单" flush>
          <OrderForm code={code} quick onCodeChange={pick} />
        </Panel>
      </div>
    </div>
  );
}

export default MarketWorkbench;
