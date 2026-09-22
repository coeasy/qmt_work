import { useMemo } from "react";
import {
  ConfirmButton,
  DataTable,
  Panel,
  TradingDateBadge,
  type Column,
} from "@/design/primitives";
import { useWatchlistStore } from "@/stores/watchlist";
import { useDisplayQuotes } from "@/hooks/useLiveQuotes";
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
import { fmtAmount, fmtPct, fmtPrice, fmtVolume, tone } from "@/shared/format";
import { isLivePrice } from "@/shared/freshness";
import type { PageProps } from "@/app/routes";
import type { Quote } from "@/shared/types";
import s from "./panels/panels.module.css";

/**
 * 表格行类型：`price` 可缺席。
 *
 * ⚠️ 修复前无行情时回填 `{ code, price: 0 }`，而 `fmtPrice(0)` 输出 **"0.00"** ——
 * 用户会把它读成「这只股真的跌到 0」，比显示 `--` 更糟（**假数据比没数据更危险**）。
 * 因此无行情的行**不给价格**，`fmtPrice(undefined)` 才是诚实的 `--`。
 */
type Row = Omit<Quote, "price"> & { price?: number };

export interface QuoteBoardProps extends Partial<PageProps> {
  /**
   * 显示「移除」列。
   *
   * ⚠️ 只有**自选股管理页**该开：这里点行是「打开该股的工作台」，多一个删除
   * 按钮就是误删入口。删除走 ConfirmButton 两段式确认，绝不用「悬浮才浮现的 ×」
   * （见 DataPanel 的教训：命中区小 + 无确认 = 点一下就没了）。
   */
  removable?: boolean;
}

/**
 * 报价牌：虚拟滚动行情表（自选股）。
 *
 * 性能设计：DataTable 走 useVirtualizer，只渲染可视行；
 * 行情数据来自 quotes store（按 code 索引），配合订阅聚合避免重复订阅。
 *
 * ★★ 本组件**只有一种点行行为**：打开该股的工作台（唯一出口 `useOpenWorkbench`）。
 * 早先它有 `onPick` / `active` / `compact` / `autoShrink` / `maxBodyHeight` 五个
 * 开关，全是为了「嵌在行情工作台右栏当切换器」这一处用法。该用法已移除 ——
 * 自选股统一由**左侧数据面板**常驻展示，工作台右栏不再重复一份，
 * 于是这五个开关一个调用方都没有了。留着它们的代价是：改表格时永远要
 * 同时推导「窄栏模式」与「收缩态」两条根本走不到的分支。需要窄栏/内嵌切换
 * 的那天再说，现在只留真实存在的用法。
 */
export function QuoteBoard({ removable = false }: QuoteBoardProps = {}) {
  const codes = useWatchlistStore((st) => st.codes);
  const remove = useWatchlistStore((st) => st.remove);
  const openWorkbench = useOpenWorkbench();
  // ★ 展示用行情：WS 实时优先，休市/未连券商时自动回退「最近交易日收盘」
  //   （直接用 useLiveQuotes 的话，休市时整表一片 `--`，会被读成软件坏了）
  const quotes = useDisplayQuotes(codes);

  const rows = useMemo<Row[]>(
    () => codes.map((c) => ({ ...(quotes[c] ?? { code: c }), code: c })),
    [codes, quotes],
  );

  /**
   * 「真有数据」= 至少一只拿到了**价格**（WS 实时 或 最近交易日收盘）或名称。
   *
   * ⚠️ 判价格必须走唯一实现 `isLivePrice`（`0` 与缺失都算没行情）——
   *    直接 `r.price !== undefined` 会把 `price: 0` 误判成「有数据」。
   *
   * 用途：整表一片 `--` 时**说明原因**（见下方 `extra`）。空白会被读成
   * 「软件坏了」，一行文案才是可操作的空状态。
   */
  const hasQuote = rows.some((r) => isLivePrice(r.price) || r.name);

  const columns: Column<Row>[] = [
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
      render: (r) => r.name || r.code || "--",
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
      render: (r: Row) => fmtPrice(r.change),
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
      // ★ bid/ask 是标量（不是数组）；`r.bid?.[0]` 恒 undefined → 买一恒 "--"
      render: (r: Row) => fmtPrice(r.bid ?? r.bids?.[0]?.price),
    },
    {
      key: "ask",
      header: "卖一",
      width: 72,
      align: "right",
      mono: true,
      render: (r: Row) => fmtPrice(r.ask ?? r.asks?.[0]?.price),
    },
    {
      key: "vol",
      header: "成交量",
      width: 88,
      align: "right",
      mono: true,
      render: (r: Row) => fmtVolume(r.volume),
    },
    {
      key: "amt",
      header: "成交额",
      width: 92,
      align: "right",
      mono: true,
      render: (r: Row) => fmtAmount(r.amount),
    },
    {
      key: "time",
      header: "时间",
      width: 76,
      align: "right",
      mono: true,
      render: (r: Row) => r.time ?? "--",
    },
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
      title={`自选 · ${codes.length} 只`}
      extra={
        <span
          style={{
            fontSize: "var(--font-xs)",
            color: "var(--text-faint)",
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          {/* 报价牌同样要标注数据日期：非交易日这里显示的是上一交易日收盘价 */}
          <TradingDateBadge />
          {/* 一只行情都没拿到时**说明原因**：整表 `--` 会被读成「软件坏了」 */}
          {codes.length > 0 && !hasQuote && (
            <span style={{ color: "var(--warning)" }}>尚未订阅到行情</span>
          )}
          <span>单击打开工作台</span>
        </span>
      }
    >
      {codes.length === 0 ? (
        // ★ 空态必须说明「怎么加自选」，而不是留一块白板：
        //   空白会被读成「软件坏了」，一行文案才是可操作的空状态。
        <div className={s.shrunkRow}>
          <span>自选股为空 —— 在行情工作台输入代码后点「加自选」</span>
        </div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.code}
          rowTone={(r) => tone(r.change_pct)}
          onRowClick={(r) =>
            // ★ 点行一律进「行情工作台」（K 线 + 分时 + 盘口 + 成交流 + 下单同屏），
            //   而不是只开一个 K 线页 —— 用户点一只票的意图是「看这只票」，
            //   不是「只看它的 K 线」。工作台内部仍可「独立打开」各子页。
            //   唯一出口 `useOpenWorkbench`（自带 normalizeCode）。
            openWorkbench(r.code, r.name ?? "")
          }
        />
      )}
    </Panel>
  );
}

export default QuoteBoard;
