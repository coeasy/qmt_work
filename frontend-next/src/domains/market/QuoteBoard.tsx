import { useMemo } from "react";
import { ConfirmButton, DataTable, Panel, type Column } from "@/design/primitives";
import { useWatchlistStore } from "@/stores/watchlist";
import { useWorkspaceStore } from "@/stores/workspace";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtAmount, fmtPct, fmtPrice, fmtVolume, tone } from "@/shared/format";
import type { PageProps } from "@/app/routes";
import type { Quote } from "@/shared/types";

/**
 * 表格行类型：`price` 可缺席。
 *
 * ⚠️ 修复前无行情时回填 `{ code, price: 0 }`，而 `fmtPrice(0)` 输出 **"0.00"** ——
 * 用户会把它读成「这只股真的跌到 0」，比显示 `--` 更糟（**假数据比没数据更危险**）。
 * 因此无行情的行**不给价格**，`fmtPrice(undefined)` 才是诚实的 `--`。
 */
type Row = Omit<Quote, "price"> & { price?: number };

export interface QuoteBoardProps extends Partial<PageProps> {
  /** 给出则点击行走回调（工作台内切换标的），否则打开独立 K 线页 */
  onPick?: (code: string, name: string) => void;
  /** 高亮行（工作台当前标的代码） */
  active?: string;
  /** 窄栏模式：只保留 代码/名称/最新/涨幅 */
  compact?: boolean;
  /**
   * 显示「移除」列。
   *
   * ⚠️ 只有**自选股管理页**该开：报价牌同时被行情工作台左栏复用，那里点行是
   * 「切换标的」，多一个删除按钮就是误删入口。删除走 ConfirmButton 两段式确认，
   * 绝不用「悬浮才浮现的 ×」（见 DataPanel 的教训：命中区小 + 无确认 = 点一下就没了）。
   */
  removable?: boolean;
}

/**
 * 报价牌：虚拟滚动行情表。
 *
 * 性能设计：DataTable 走 useVirtualizer，只渲染可视行；
 * 行情数据来自 quotes store（按 code 索引），配合订阅聚合避免重复订阅。
 *
 * 两种用法（**同一份实现**，避免工作台与独立页各写一套而漂移）：
 * - 独立页：默认行为，点行打开独立 K 线 Tab；
 * - 行情工作台左栏：传 `onPick` + `active`，点行只在工作台内切换标的，
 *   并用 `active` 高亮当前标的（否则用户看不出「现在看的是哪只」）。
 * `compact` 用于 240~300px 的窄栏：去掉成交量/成交额/时间等宽列，
 * 否则列宽合计约 780px，窄栏下只能横向滚动、看不到涨跌。
 */
export function QuoteBoard({
  onPick,
  active,
  compact = false,
  removable = false,
}: QuoteBoardProps = {}) {
  const codes = useWatchlistStore((st) => st.codes);
  const remove = useWatchlistStore((st) => st.remove);
  const open = useWorkspaceStore((st) => st.open);
  // 订阅 + 取值一条龙（漏订阅不会报错，只会让价格永远停在「--」）
  const quotes = useLiveQuotes(codes);

  const rows = useMemo<Row[]>(
    () => codes.map((c) => quotes[c] ?? { code: c }),
    [codes, quotes],
  );

  const columns: Column<Row>[] = [
    {
      key: "code",
      header: "代码",
      width: compact ? 80 : 92,
      mono: true,
      render: (r) => r.code,
    },
    {
      key: "name",
      header: "名称",
      width: compact ? 70 : 90,
      render: (r) => r.name ?? "--",
    },
    {
      key: "price",
      header: "最新",
      width: compact ? 66 : 76,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(r.price),
    },
    ...(compact
      ? []
      : ([
          {
            key: "chg",
            header: "涨跌",
            width: 68,
            align: "right",
            mono: true,
            render: (r: Quote) => fmtPrice(r.change),
          },
        ] as Column<Row>[])),
    {
      key: "pct",
      header: "涨幅",
      width: compact ? 64 : 72,
      align: "right",
      mono: true,
      render: (r) => fmtPct(r.change_pct),
    },
    ...(compact
      ? []
      : ([
          {
            key: "bid",
            header: "买一",
            width: 72,
            align: "right",
            mono: true,
            // ★ bid/ask 是标量（不是数组）；`r.bid?.[0]` 恒 undefined → 买一恒 "--"
            render: (r: Quote) => fmtPrice(r.bid ?? r.bids?.[0]?.price),
          },
          {
            key: "ask",
            header: "卖一",
            width: 72,
            align: "right",
            mono: true,
            render: (r: Quote) => fmtPrice(r.ask ?? r.asks?.[0]?.price),
          },
          {
            key: "vol",
            header: "成交量",
            width: 88,
            align: "right",
            mono: true,
            render: (r: Quote) => fmtVolume(r.volume),
          },
          {
            key: "amt",
            header: "成交额",
            width: 92,
            align: "right",
            mono: true,
            render: (r: Quote) => fmtAmount(r.amount),
          },
          {
            key: "time",
            header: "时间",
            width: 76,
            align: "right",
            mono: true,
            render: (r: Quote) => r.time ?? "--",
          },
        ] as Column<Row>[])),
    ...(removable
      ? ([
          {
            key: "_remove",
            header: "操作",
            width: 84,
            align: "right",
            render: (r: Row) => (
              <ConfirmButton
                size="sm"
                variant="ghost"
                confirmText="确认移除"
                title={`从自选股移除 ${r.code}`}
                onConfirm={() => remove(r.code)}
              >
                移除
              </ConfirmButton>
            ),
          },
        ] as Column<Row>[])
      : []),
  ];

  return (
    <Panel
      flush
      title={`报价牌 · ${codes.length} 只`}
      extra={
        <span style={{ fontSize: "var(--font-xs)", color: "var(--text-faint)" }}>
          {onPick ? "单击切换标的" : "单击打开 K 线"}
        </span>
      }
    >
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.code}
        rowTone={(r) => tone(r.change_pct)}
        activeKey={active}
        onRowClick={(r) =>
          onPick
            ? onPick(r.code, r.name ?? "")
            : open("quote", { code: r.code, name: r.name ?? "" }, { title: r.name ?? r.code })
        }
      />
    </Panel>
  );
}

export default QuoteBoard;
