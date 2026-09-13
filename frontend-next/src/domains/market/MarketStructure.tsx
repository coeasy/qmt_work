import { useMemo, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Panel, Spinner, type Column } from "@/design/primitives";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "@/charts/echartsSetup";
import { marketApi, type OverviewResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtAmount, fmtPct, fmtPrice, toneColor } from "@/shared/format";
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

  const indexCols: Column<IndexRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "指数", render: (r) => r.name || "--" },
    {
      key: "last",
      header: "最新",
      width: 100,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(r.change_pct) }}>{fmtPrice(r.last)}</span>,
    },
    {
      key: "pct",
      header: "涨跌幅",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(r.change_pct) }}>{fmtPct(r.change_pct)}</span>,
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

      {res.loading && !d ? (
        <Spinner label="加载市场概览…" />
      ) : d ? (
        <>
          <div className={s.stats}>
            <div className={s.stat}>
              <span className={s.statLabel}>涨跌家数差</span>
              <span className={s.statValue} style={{ color: toneColor(bs?.breadth_net) }}>
                {bs?.breadth_net === null || bs?.breadth_net === undefined ? "—" : String(bs.breadth_net)}
              </span>
              <span className={s.statSub}>
                前值 {bs?.breadth_net_prev === null || bs?.breadth_net_prev === undefined ? "—" : String(bs.breadth_net_prev)}
              </span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>停板家数</span>
              <span className={s.statValue}>
                {bs?.stopped_count === null || bs?.stopped_count === undefined ? "—" : String(bs.stopped_count)}
              </span>
              <span className={s.statSub}>涨停 + 跌停（合并口径）</span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>成交均价</span>
              <span className={s.statValue}>
                {bs?.avg_price === null || bs?.avg_price === undefined ? "—" : String(bs.avg_price)}
              </span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>两市成交额</span>
              <span className={s.statValue}>
                {d.two_city_turnover === null ? "—" : fmtAmount(d.two_city_turnover)}
              </span>
              <span className={s.statSub}>上证 + 深证</span>
            </div>
          </div>

          {bs?.note && <div className={`${s.note} ${s.noteInfo}`}>{bs.note}</div>}

          <div className={s.cols2}>
            <Panel flush title={`主要指数（${d.indices.length}）`} className={s.grow}>
              <div className={s.tableArea} style={{ maxHeight: 260 }}>
                {d.indices.length === 0 ? (
                  <EmptyState text="无指数快照" />
                ) : (
                  <DataTable columns={indexCols} rows={d.indices} rowKey={(r) => r.code} rowHeight={24} rowTone={(r) => (r.change_pct && r.change_pct > 0 ? 1 : r.change_pct && r.change_pct < 0 ? -1 : 0)} />
                )}
              </div>
            </Panel>

            <Panel title="宽度趋势（涨跌家数）" className={s.grow}>
              <div className={s.chart}>
                {trend.length === 0 ? (
                  <EmptyState text="无宽度趋势数据" />
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
                <EmptyState text="无统计数据" />
              ) : (
                <DataTable columns={breadthCols} rows={breadthRows} rowKey={(r) => r.code} rowHeight={22} />
              )}
            </div>
          </Panel>

          <div className={s.note}>{d.two_city_note}</div>
        </>
      ) : (
        <EmptyState text="无市场概览数据" />
      )}
    </div>
  );
}

export default MarketStructure;
