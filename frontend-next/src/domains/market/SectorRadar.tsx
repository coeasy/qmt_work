import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Input,
  Panel,
  Select,
  Spinner,
  type Column,
} from "@/design/primitives";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "@/charts/echartsSetup";
import { marketApi, type BoardItem } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtAmount, fmtPct, toneColor } from "@/shared/format";
import type { Instrument } from "@/shared/types";
import s from "../domain.module.css";

/**
 * 板块雷达：板块榜 + 轮动热力图 + 成分股。
 *
 * ★ 契约要点（aggregates.py:boards / rotation / board_constituents）：
 *   - /market/boards 的 kind 只能是 industry / concept / stat；sort_by 只能是 pct / amount
 *     （非法值后端明确 400，不再静默回落，前端据此只提供合法选项）
 *   - /market/rotation 的 kind 只支持 industry / concept；days 夹取 3–20，top_n 夹取 10–60
 *   - 轮动矩阵的 daily 是逐日涨跌幅（由板块日K 计算），heatmap 的 x=日期、y=板块
 *   - 板块资金流只对 881xxx/880xxx 代码有意义（前端据此校验）
 */
export function SectorRadar() {
  const [kind, setKind] = useState("industry");
  const [sortBy, setSortBy] = useState("pct");
  const [days, setDays] = useState("10");
  const [selected, setSelected] = useState<BoardItem | null>(null);

  const boards = useAsync(() => marketApi.boards(kind, sortBy, 60), [kind, sortBy]);
  const rotation = useAsync(() => marketApi.rotation(Number(days) || 10, kind, 30), [days, kind]);
  const cons = useAsync<{ items: Instrument[]; has_more?: boolean; source?: string }>(
    () => (selected ? marketApi.boardConstituents(selected.code, 100, 0) : Promise.resolve({ items: [] })),
    [selected?.code],
  );

  const rot = rotation.data?.boards ?? [];

  /** 轮动热力图：x=交易日，y=板块，色=当日涨跌幅 */
  const heatOption = useMemo<EChartsOption>(() => {
    if (rot.length === 0) return {};
    const dates = Array.from(new Set(rot.flatMap((b) => b.daily.map((d) => d.date)))).sort();
    const names = rot.map((b) => b.name || b.code);
    const data: Array<[number, number, number]> = [];
    rot.forEach((b, yi) => {
      b.daily.forEach((d) => {
        const xi = dates.indexOf(d.date);
        if (xi >= 0) data.push([xi, yi, d.pct]);
      });
    });
    const maxAbs = Math.max(1, ...data.map((d) => Math.abs(d[2])));

    return {
      animation: false,
      backgroundColor: "transparent",
      tooltip: {
        position: "top",
        formatter: (p: unknown) => {
          const params = p as { value: [number, number, number] };
          const [xi, yi, v] = params.value;
          return `${names[yi]}<br/>${dates[xi]}<br/>${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
        },
      },
      grid: { left: 96, right: 60, top: 8, bottom: 40 },
      xAxis: {
        type: "category",
        data: dates,
        axisLabel: { color: "var(--chart-axis)", fontSize: 10, rotate: 45 },
        splitArea: { show: true },
      },
      yAxis: {
        type: "category",
        data: names,
        axisLabel: { color: "var(--chart-axis)", fontSize: 10 },
      },
      visualMap: {
        min: -maxAbs,
        max: maxAbs,
        calculable: false,
        orient: "vertical",
        right: 4,
        top: "center",
        textStyle: { color: "var(--chart-axis)", fontSize: 10 },
        inRange: { color: ["#22c55e", "#1a2230", "#ef4444"] },
      },
      series: [
        {
          type: "heatmap",
          data,
          label: { show: false },
          emphasis: { itemStyle: { borderColor: "var(--accent)", borderWidth: 1 } },
        },
      ],
    };
  }, [rot]);

  const boardCols: Column<BoardItem>[] = [
    { key: "code", header: "代码", width: 90, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", render: (r) => r.name },
    {
      key: "pct",
      header: "涨跌幅",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(r.change_pct) }}>{fmtPct(r.change_pct)}</span>,
    },
    { key: "amount", header: "成交额", width: 96, align: "right", mono: true, render: (r) => fmtAmount(r.amount) },
  ];

  const consCols: Column<Instrument>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", render: (r) => r.name },
    { key: "exchange", header: "交易所", width: 80, render: (r) => r.exchange ?? "--" },
    { key: "category", header: "类别", width: 100, render: (r) => r.category ?? "--" },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <span className={s.muted}>板块类型</span>
        <Select
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          options={[
            { value: "industry", label: "行业（881xxx）" },
            { value: "concept", label: "概念（880xxx）" },
            { value: "stat", label: "统计类" },
          ]}
        />
        <span className={s.muted}>排序</span>
        <Select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
          options={[
            { value: "pct", label: "按涨跌幅" },
            { value: "amount", label: "按成交额" },
          ]}
        />
        <span className={s.muted}>轮动回看</span>
        <Input value={days} onChange={(e) => setDays(e.target.value)} mono style={{ width: 60 }} />
        <span className={s.muted}>天</span>
        <span className={s.spacer} />
        {boards.data && <Badge tone="info">源 {boards.data.source}</Badge>}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            void boards.reload();
            void rotation.reload();
          }}
        >
          刷新
        </Button>
      </div>

      <div className={s.splitWide}>
        <Panel flush title="板块轮动热力图" className={s.grow}>
          <div className={s.chart}>
            {rotation.loading && rot.length === 0 ? (
              <Spinner label="计算轮动矩阵…" />
            ) : rotation.error ? (
              <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                {rotation.error}
              </div>
            ) : rot.length === 0 ? (
              <EmptyState text="无轮动数据" />
            ) : (
              <EChart option={heatOption} />
            )}
          </div>
        </Panel>

        <Panel flush title={`板块榜（${boards.data?.count ?? 0}）`} className={s.grow}>
          <div className={s.tableArea}>
            {boards.loading && !boards.data ? (
              <Spinner label="加载中…" />
            ) : boards.error ? (
              <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                {boards.error}
              </div>
            ) : (boards.data?.items.length ?? 0) === 0 ? (
              <EmptyState text="无板块数据" />
            ) : (
              <DataTable
                columns={boardCols}
                rows={boards.data?.items ?? []}
                rowKey={(r) => r.code}
                rowHeight={24}
                activeKey={selected?.code}
                onRowClick={(r) => setSelected(r)}
              />
            )}
          </div>
        </Panel>
      </div>

      {selected && (
        <Panel
          flush
          title={`成分股 · ${selected.name}（${selected.code}）`}
          extra={
            <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>
              关闭
            </Button>
          }
        >
          <div className={s.tableArea} style={{ maxHeight: 260 }}>
            {cons.loading && cons.data?.items.length === 0 ? (
              <Spinner label="加载成分股…" />
            ) : cons.error ? (
              <div className={`${s.note} ${s.noteWarn}`} style={{ margin: 8 }}>
                {cons.error}
              </div>
            ) : (cons.data?.items.length ?? 0) === 0 ? (
              <EmptyState text="该板块无成分数据（统计类板块不含成分股）" />
            ) : (
              <DataTable
                columns={consCols}
                rows={cons.data?.items ?? []}
                rowKey={(r) => r.code}
                rowHeight={22}
              />
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}

export default SectorRadar;
