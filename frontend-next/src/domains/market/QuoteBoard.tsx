import { useMemo } from "react";
import { DataTable, Panel, type Column } from "@/design/primitives";
import { useWatchlistStore } from "@/stores/watchlist";
import { useQuotesStore } from "@/stores/quotes";
import { useWorkspaceStore } from "@/stores/workspace";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtAmount, fmtPct, fmtPrice, fmtVolume, tone } from "@/shared/format";
import type { Quote } from "@/shared/types";

/**
 * 报价牌：虚拟滚动行情表。
 *
 * 性能设计：DataTable 走 useVirtualizer，只渲染可视行；
 * 行情数据来自 quotes store（按 code 索引），配合订阅聚合避免重复订阅。
 */
export function QuoteBoard() {
  const codes = useWatchlistStore((st) => st.codes);
  const quotes = useQuotesStore((st) => st.quotes);
  const open = useWorkspaceStore((st) => st.open);

  const stable = useMemo(() => codes, [codes]);
  useQuoteSubscription(stable);

  const rows = useMemo(
    () => codes.map((c) => quotes[c] ?? ({ code: c, price: 0 } as Quote)),
    [codes, quotes],
  );

  const columns: Column<Quote>[] = [
    {
      key: "code",
      header: "代码",
      width: 92,
      mono: true,
      render: (r) => r.code,
    },
    {
      key: "name",
      header: "名称",
      width: 90,
      render: (r) => r.name ?? "--",
    },
    {
      key: "price",
      header: "最新",
      width: 76,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(r.price),
    },
    {
      key: "chg",
      header: "涨跌",
      width: 68,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(r.change),
    },
    {
      key: "pct",
      header: "涨幅",
      width: 72,
      align: "right",
      mono: true,
      render: (r) => fmtPct(r.change_pct),
    },
    {
      key: "bid",
      header: "买一",
      width: 72,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(r.bid?.[0]),
    },
    {
      key: "ask",
      header: "卖一",
      width: 72,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(r.ask?.[0]),
    },
    {
      key: "vol",
      header: "成交量",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => fmtVolume(r.volume),
    },
    {
      key: "amt",
      header: "成交额",
      width: 92,
      align: "right",
      mono: true,
      render: (r) => fmtAmount(r.amount),
    },
    {
      key: "time",
      header: "时间",
      width: 76,
      align: "right",
      mono: true,
      render: (r) => r.time ?? "--",
    },
  ];

  return (
    <Panel
      flush
      title={`报价牌 · ${codes.length} 只`}
      extra={<span style={{ fontSize: "var(--font-xs)", color: "var(--text-faint)" }}>双击打开 K 线</span>}
    >
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.code}
        rowTone={(r) => tone(r.change_pct)}
        onRowClick={(r) => open("quote", { code: r.code, name: r.name ?? "" }, { title: r.name ?? r.code })}
      />
    </Panel>
  );
}

export default QuoteBoard;
