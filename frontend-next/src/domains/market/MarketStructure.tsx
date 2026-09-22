import { useMemo, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Panel, Spinner, type Column } from "@/design/primitives";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "@/charts/echartsSetup";
import { marketApi, type OverviewResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtAmount, fmtPct, fmtPrice, toneColor } from "@/shared/format";
import { isLivePrice } from "@/shared/freshness";
import s from "../domain.module.css";

type BreadthRow = OverviewResponse["breadth"][number];
type IndexRow = OverviewResponse["indices"][number];

/**
 * 市场结构（广度 / 指数 / 宽度趋势 / 两市成交额）。
 *
 * ★ 契约要点（aggregates.py:overview）：
 *   - 返回 {breadth, breadth_summary, indices, breadth_trend, two_city_turnover, two_city_note, ...}
 *   - breadth_summary.breadth_net = 上涨家数 - 下跌家数（TDX 统计口径），
 *     **不是**分别的上涨/下跌家数 —— TDX 统计板块只提供涨跌差与停板家数，本页不拆分伪造
 *   - two_city_turnover 由上证 + 深证指数快照 amount 求和；任一缺失则为 null，显示「—」
 *   - breadth_trend 是涨跌家数日K（真实家数时间序列），可能为 null
 */
export function MarketStructure() {
  const [showAll, setShowAll] = useState(false);
  const res = useAsync(() => marketApi.overview(), []);
  const d = res.data;

  const trend = d?.breadth_trend?.series ?? [];

  const trendOption = useMemo<EChartsOption>(() => {
    return {
      animation: false,
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      grid: { left: 60, right: 16, top: 16, bottom: 32 },
      xAxis: {
        type: "category",
        data: trend.map((p) => p.date),
        axisLabel: { color: "var(--chart-axis)", fontSize: 10, rotate: 45 },
        axisLine: { lineStyle: { color: "var(--chart-grid)" } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "var(--chart-axis)", fontSize: 10 },
        splitLine: { lineStyle: { color: "var(--chart-grid)" } },
      },
      series: [
        {
          name: "涨跌家数",
          type: "bar",
          data: trend.map((p) => ({
            value: p.value,
            itemStyle: { color: p.value >= 0 ? "var(--up)" : "var(--down)" },
          })),
        },
      ],
    };
  }, [trend]);

  const breadthCols: Column<BreadthRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "统计项", render: (r) => r.name || "--" },
    {
      key: "count",
      header: "数值",
      width: 110,
      align: "right",
      mono: true,
      render: (r) => (r.count === null || r.count === undefined ? "—" : String(r.count)),
    },
    { key: "metric", header: "口径", width: 120, render: (r) => r.metric ?? "--" },
    { key: "unit", header: "单位", width: 80, render: (r) => r.unit ?? "--" },
  ];

  // 指数快照同样是**查询那一刻**的值 ⇒ 叠加实时行情，否则必须手动刷新才动。
  const indexCodes = useMemo(() => (d?.indices ?? []).map((r) => r.code), [d?.indices]);
  const indexQuotes = useLiveQuotes(indexCodes);
  const indexLast = (r: IndexRow): number | undefined => {
    const q = indexQuotes[r.code]?.price;
    if (isLivePrice(q)) return q;
    return isLivePrice(r.last) ? r.last : undefined;
  };

  const indexCols: Column<IndexRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "指数", render: (r) => r.name || "--" },
    {
      key: "last",
      header: "最新",
      width: 100,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = indexQuotes[r.code]?.change_pct ?? r.change_pct;
        return <span style={{ color: toneColor(pct) }}>{fmtPrice(indexLast(r))}</span>;
      },
    },
    {
      key: "pct",
      header: "涨跌幅",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = indexQuotes[r.code]?.change_pct ?? r.change_pct;
        return <span style={{ color: toneColor(pct) }}>{fmtPct(pct)}</span>;
      },
    },
    { key: "amount", header: "成交额", width: 110, align: "right", mono: true, render: (r) => fmtAmount(r.amount) },
  ];

  const breadthRows = showAll ? (d?.breadth ?? []) : (d?.breadth ?? []).slice(0, 8);
  const bs = d?.breadth_summary;

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Button size="sm" variant="ghost" onClick={() => void res.reload()}>
          刷新
        </Button>
        <span className={s.spacer} />
        {d?.source && <Badge tone="info">源 {d.source}</Badge>}
        {d?.ts && <span className={s.muted}>更新于 {d.ts}</span>}
      </div>

      {res.error && <div className={`${s.note} ${s.noteError}`}>{res.error}</div>}

      {/* ★ 后端已给出「为什么三块都空」（数据源能力/连接问题），必须原样转述。
          没有这句时，页面只剩一排「—」和三个「无X」，用户只能猜是软件坏了。 */}
      {d?.unavailable && (
        <div className={`${s.note} ${s.noteWarn}`}>
          {d.unavailable}
          <span className={s.muted}>
            （指数 / 涨跌家数 / 统计板块均依赖行情源，离线日线数据不参与本页统计）
          </span>
        </div>
      )}

      {res.loading && !d ? (
        <Spinner label="加载市场概览…" />
      ) : d ? (
        <>
          <div className={s.statRow}>
            <div
              className={s.statRowItem}
              title={`前值 ${bs?.breadth_net_prev === null || bs?.breadth_net_prev === undefined ? "—" : String(bs.breadth_net_prev)}`}
            >
              <span className={s.statRowLabel}>涨跌家数差</span>
              <span className={s.statRowValue} style={{ color: toneColor(bs?.breadth_net) }}>
                {bs?.breadth_net === null || bs?.breadth_net === undefined ? "—" : String(bs.breadth_net)}
              </span>
            </div>
            <div className={s.statRowItem} title="涨停 + 跌停（合并口径）">
              <span className={s.statRowLabel}>停板家数</span>
              <span className={s.statRowValue}>
                {bs?.stopped_count === null || bs?.stopped_count === undefined ? "—" : String(bs.stopped_count)}
              </span>
            </div>
            <div className={s.statRowItem}>
              <span className={s.statRowLabel}>成交均价</span>
              <span className={s.statRowValue}>
                {bs?.avg_price === null || bs?.avg_price === undefined ? "—" : String(bs.avg_price)}
              </span>
            </div>
            <div className={s.statRowItem} title="上证 + 深证指数成交额求和；任一缺失则为 —">
              <span className={s.statRowLabel}>两市成交额</span>
              <span className={s.statRowValue}>
                {d.two_city_turnover === null ? "—" : fmtAmount(d.two_city_turnover)}
              </span>
            </div>
          </div>

          {bs?.note && <div className={`${s.note} ${s.noteInfo}`}>{bs.note}</div>}

          <div className={s.cols2}>
            <Panel flush title={`主要指数（${d.indices.length}）`} className={s.grow}>
              <div className={s.tableArea} style={{ maxHeight: 260 }}>
                {d.indices.length === 0 ? (
                  <EmptyState
                    text={
                      d.unavailable
                        ? "无指数快照 —— 成因见上方提示条"
                        : "无指数快照 —— 指数快照来自行情源，取不到时为空；可点「刷新」重试"
                    }
                  />
                ) : (
                  <DataTable
                    columns={indexCols}
                    rows={d.indices}
                    rowKey={(r) => r.code}
                    rowHeight={24}
                    rowTone={(r) => {
                      // 行底色也要跟着实时值走，否则「数字绿了、底色还红」
                      const pct = indexQuotes[r.code]?.change_pct ?? r.change_pct;
                      return pct && pct > 0 ? 1 : pct && pct < 0 ? -1 : 0;
                    }}
                  />
                )}
              </div>
            </Panel>

            <Panel title="宽度趋势（涨跌家数）" className={s.grow}>
              <div className={s.chart}>
                {trend.length === 0 ? (
                  <EmptyState
                    text={
                      d.unavailable
                        ? "无宽度趋势数据 —— 成因见上方提示条"
                        : "无宽度趋势数据 —— 涨跌家数日线来自行情源板块接口，取不到时为空"
                    }
                  />
                ) : (
                  <EChart option={trendOption} />
                )}
              </div>
            </Panel>
          </div>

          <Panel
            flush
            title={`统计类板块（${d.breadth.length}）`}
            extra={
              d.breadth.length > 8 ? (
                <Button size="sm" variant="ghost" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? "收起" : "展开全部"}
                </Button>
              ) : null
            }
          >
            <div className={s.tableArea} style={{ maxHeight: 260 }}>
              {d.breadth.length === 0 ? (
                <EmptyState
                  text={
                    d.unavailable
                      ? "无统计数据 —— 成因见上方提示条"
                      : "无统计数据 —— 统计板块（涨跌家数/停板家数/成交均价）来自行情源，取不到时为空"
                  }
                />
              ) : (
                <DataTable columns={breadthCols} rows={breadthRows} rowKey={(r) => r.code} rowHeight={22} />
              )}
            </div>
          </Panel>

          <div className={s.note}>{d.two_city_note}</div>
        </>
      ) : (
        // 只写「无数据」会让人以为程序坏了；这里给出可执行动作，并把
        // 「后端若给了原因会显示在顶部」讲明，避免替用户瞎猜原因。
        <EmptyState
          text="无市场概览数据 —— 可先刷新重试；若后端给出了具体原因，会显示在顶部提示条里"
          actionText="刷新"
          onAction={() => void res.reload()}
        />
      )}
    </div>
  );
}

export default MarketStructure;
