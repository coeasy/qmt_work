import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Input,
  Panel,
  Spinner,
  Tabs,
  type Column,
} from "@/design/primitives";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "@/charts/echartsSetup";
import { marketApi, type BoardMoneyflowResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtAmount, fmtPct, fmtPrice, normalizeCode, toneColor } from "@/shared/format";
import s from "../domain.module.css";

/**
 * 资金流。
 *
 * ★ 契约要点（aggregates.py:moneyflow / board_moneyflow）：
 *   - 个股：{code, inside(内盘手), outside(外盘手), net(外盘-内盘), strength(分钟主买/主卖),
 *     volume_ratio(量比), est:false, ts, source}；任一字段缺失返回 null → 显示「—」，禁止估算
 *   - 板块：{code, count, total_net, total_inside, total_outside, contributors:[{code,name,net,pct}],
 *     granularity:"day", source, ts}；granularity 固定 day，因为 eltdx 只有日累计快照
 *   - 板块资金流仅对 881xxx / 880xxx 代码有意义，其他代码后端直接 400
 */
export function MoneyFlow() {
  const [tab, setTab] = useState<"stock" | "board">("stock");

  const [code, setCode] = useState("000001.SZ");
  const [boardCode, setBoardCode] = useState("881101.SH");
  const [topN, setTopN] = useState("30");

  const normCode = useMemo(() => normalizeCode(code), [code]);
  const mf = useAsync(() => marketApi.moneyflow(normCode), [normCode]);
  const bmf = useAsync<BoardMoneyflowResponse>(
    () => marketApi.boardMoneyflow(boardCode, "auto", Number(topN) || 30),
    [boardCode, topN],
  );

  const d = mf.data;
  const strength = d?.strength ?? [];

  const strengthOption = useMemo<EChartsOption>(() => {
    const times = strength.map((p) => p.t ?? "");
    const buys = strength.map((p) => p.buy ?? 0);
    const sells = strength.map((p) => p.sell ?? 0);
    return {
      animation: false,
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      legend: { textStyle: { color: "var(--chart-axis)", fontSize: 10 }, top: 0 },
      grid: { left: 56, right: 16, top: 28, bottom: 36 },
      xAxis: {
        type: "category",
        data: times,
        axisLabel: { color: "var(--chart-axis)", fontSize: 10, interval: 29 },
        axisLine: { lineStyle: { color: "var(--chart-grid)" } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "var(--chart-axis)", fontSize: 10 },
        splitLine: { lineStyle: { color: "var(--chart-grid)" } },
      },
      series: [
        { name: "主买", type: "line", data: buys, showSymbol: false, lineStyle: { color: "var(--up)", width: 1.2 } },
        { name: "主卖", type: "line", data: sells, showSymbol: false, lineStyle: { color: "var(--down)", width: 1.2 } },
      ],
    };
  }, [strength]);

  // 贡献度排名只有资金流，没有价格 ⇒ 叠加实时行情，才能一眼看出
  // 「资金在流入的票，是不是也在涨」（流入但下跌 = 典型的主力对倒/出货信号）。
  const contribCodes = useMemo(
    () => (bmf.data?.contributors ?? []).map((r) => r.code),
    [bmf.data],
  );
  const contribQuotes = useLiveQuotes(contribCodes);

  const contributorCols: Column<BoardMoneyflowResponse["contributors"][number]>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 120, render: (r) => r.name || "--" },
    {
      key: "last",
      header: "最新",
      width: 80,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(contribQuotes[r.code]?.price),
    },
    {
      key: "chg",
      header: "涨跌幅",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = contribQuotes[r.code]?.change_pct;
        return <span style={{ color: toneColor(pct) }}>{fmtPct(pct)}</span>;
      },
    },
    {
      key: "net",
      header: "净流入(手)",
      width: 110,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(r.net) }}>{fmtAmount(r.net)}</span>,
    },
    {
      key: "pct",
      header: "贡献度",
      width: 90,
      align: "right",
      mono: true,
      render: (r) => fmtPct(r.pct),
    },
  ];

  return (
    <div className={s.page}>
      <Tabs
        items={[
          { key: "stock", label: "个股资金流" },
          { key: "board", label: "板块资金流" },
        ]}
        value={tab}
        onChange={setTab}
      />

      {tab === "stock" ? (
        <>
          <div className={s.toolbar}>
            <Input value={code} onChange={(e) => setCode(e.target.value)} mono style={{ width: 150 }} />
            <Button size="sm" variant="ghost" onClick={() => void mf.reload()}>
              查询
            </Button>
            <span className={s.spacer} />
            {d?.source && <Badge tone="info">源 {d.source}</Badge>}
            {d?.est === false && <Badge tone="success">真实口径</Badge>}
          </div>

          {mf.error && (
            <div className={`${s.note} ${s.noteWarn}`}>
              {mf.error}
              <div style={{ marginTop: 4 }}>
                资金流依赖 TDX 公共行情的内外盘快照与分钟买卖力道；非交易时段或代码不受支持时无数据（零 mock）。
              </div>
            </div>
          )}

          {mf.loading && !d ? (
            <Spinner label="加载资金流…" />
          ) : d ? (
            <>
              <div className={s.stats}>
                <div className={s.stat}>
                  <span className={s.statLabel}>外盘（主买，手）</span>
                  <span className={s.statValue} style={{ color: "var(--up)" }}>
                    {fmtAmount(d.outside)}
                  </span>
                </div>
                <div className={s.stat}>
                  <span className={s.statLabel}>内盘（主卖，手）</span>
                  <span className={s.statValue} style={{ color: "var(--down)" }}>
                    {fmtAmount(d.inside)}
                  </span>
                </div>
                <div className={s.stat}>
                  <span className={s.statLabel}>净流入（外-内）</span>
                  <span className={s.statValue} style={{ color: toneColor(d.net) }}>
                    {fmtAmount(d.net)}
                  </span>
                </div>
                <div className={s.stat}>
                  <span className={s.statLabel}>量比</span>
                  <span className={s.statValue}>
                    {d.volume_ratio === null || d.volume_ratio === undefined ? "—" : d.volume_ratio.toFixed(2)}
                  </span>
                  <span className={s.statSub}>{d.ts ?? ""}</span>
                </div>
              </div>

              <Panel title="分钟买卖力道" className={s.grow}>
                <div className={s.chart}>
                  {strength.length === 0 ? (
                    <EmptyState text="无分钟级买卖力道数据" />
                  ) : (
                    <EChart option={strengthOption} />
                  )}
                </div>
              </Panel>
            </>
          ) : (
            <EmptyState text="无数据" />
          )}
        </>
      ) : (
        <>
          <div className={s.toolbar}>
            <Input
              value={boardCode}
              onChange={(e) => setBoardCode(e.target.value)}
              mono
              style={{ width: 150 }}
              placeholder="881xxx / 880xxx"
            />
            <Input value={topN} onChange={(e) => setTopN(e.target.value)} mono style={{ width: 60 }} />
            <span className={s.muted}>贡献股数</span>
            <Button size="sm" variant="ghost" onClick={() => void bmf.reload()}>
              查询
            </Button>
            <span className={s.spacer} />
            {bmf.data && <Badge tone="info">粒度 {bmf.data.granularity}</Badge>}
          </div>

          {bmf.error && (
            <div className={`${s.note} ${s.noteWarn}`}>
              {bmf.error}
              <div style={{ marginTop: 4 }}>
                板块资金流仅支持 TDX 板块指数代码（881xxx 行业 / 880xxx 概念·统计），
                其他代码会被后端显式拒绝，避免触发全市场回落聚合（&gt;90s）。
              </div>
            </div>
          )}

          {bmf.loading && !bmf.data ? (
            <Spinner label="聚合成分股资金流…" />
          ) : bmf.data ? (
            <>
              <div className={s.stats}>
                <div className={s.stat}>
                  <span className={s.statLabel}>板块净流入（手）</span>
                  <span className={s.statValue} style={{ color: toneColor(bmf.data.total_net) }}>
                    {fmtAmount(bmf.data.total_net)}
                  </span>
                </div>
                <div className={s.stat}>
                  <span className={s.statLabel}>内盘合计</span>
                  <span className={s.statValue}>{fmtAmount(bmf.data.total_inside)}</span>
                </div>
                <div className={s.stat}>
                  <span className={s.statLabel}>外盘合计</span>
                  <span className={s.statValue}>{fmtAmount(bmf.data.total_outside)}</span>
                </div>
                <div className={s.stat}>
                  <span className={s.statLabel}>有效成分股</span>
                  <span className={s.statValue}>{bmf.data.count}</span>
                  <span className={s.statSub}>更新于 {bmf.data.ts}</span>
                </div>
              </div>

              <Panel flush className={s.grow} title={`贡献度排名（Top ${bmf.data.contributors.length}）`}>
                <div className={s.tableArea}>
                  {bmf.data.contributors.length === 0 ? (
                    <EmptyState text="无有效成分股资金流数据" />
                  ) : (
                    <DataTable
                      columns={contributorCols}
                      rows={bmf.data.contributors}
                      rowKey={(r) => r.code}
                      rowHeight={24}
                      rowTone={(r) => (r.net > 0 ? 1 : r.net < 0 ? -1 : 0)}
                    />
                  )}
                </div>
              </Panel>
            </>
          ) : (
            <EmptyState text="无数据" />
          )}
        </>
      )}
    </div>
  );
}

export default MoneyFlow;
