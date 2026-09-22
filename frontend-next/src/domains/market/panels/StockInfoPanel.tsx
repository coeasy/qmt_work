import { Badge, EmptyState, Spinner } from "@/design/primitives";
import { marketApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtAmount, fmtPrice } from "@/shared/format";
import s from "./panels.module.css";

/**
 * 股票基本信息面板（行情工作台**右栏**的「基本信息」）。
 *
 * ★ 契约要点（`market.py:market_stock_info`）：
 *   - `GET /market/stock-info?code=&conn_id=&source=` → 名称 / 交易所 / 板块 /
 *     涨跌停 / 昨收 / 行业 / 概念 / 行情派生字段 / source
 *   - 代码会被后端补交易所后缀；**裸代码查不到中文名**（界面只剩数字）
 *   - 全部源不可用时返回 `note` 字段说明「板块是按代码前缀推断的」——
 *     这种降级必须显示给用户，否则「推断值」会被当成事实
 *   - 数值字段可能为 `null`：显示「--」，**绝不显示 0**
 *
 * ★ 2026-09-21 扩展（「补充更多个股的基本信息字段」）：
 *   市值 / 市盈率 / 市净率 / 换手率 / 振幅 / 量比 / 均价 / 今开 / 最高 / 最低。
 *   这些字段**腾讯快照里本来就有**（后端顺手解析透出，**零额外请求**）；
 *   换到拿不到它们的源（eltdx 本地库 / 券商）时后端给 `null`，这里显示 `--`。
 *
 * ⚠️ 百分比有两套语义，别混用：
 *   - 涨跌用 `fmtPct`（带正负号，`+0.78%`）；
 *   - 振幅 / 换手率是**无符号比率**（显示 `+0.77%` 很怪），走本地 `pct()`。
 */
export interface StockInfoPanelProps {
  code: string;
  /**
   * 面板体最大高度（右栏要留给成交流与下单），超出则内部滚动。
   *
   * ⚠️ 不给就是自然高度：底部坞与独立页那种宽度充裕的场景不该被截断。
   */
  maxBodyHeight?: number;
}

/** 无符号百分比（振幅 / 换手率）；缺失或非法一律 `--` */
function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return `${v.toFixed(digits)}%`;
}

export function StockInfoPanel({ code, maxBodyHeight }: StockInfoPanelProps) {
  const info = useAsync(() => marketApi.stockInfo(code), [code]);
  const d = info.data;

  if (info.loading && !d) return <Spinner label="读取基本信息…" />;
  if (info.error) return <div className={s.panelNote}>{info.error}</div>;
  if (!d) return <EmptyState text="无基本信息" />;

  const rows: Array<[string, React.ReactNode]> = [
    ["代码", <span className={s.mono}>{d.code}</span>],
    ["名称", d.name || "--"],
    ["交易所", d.exchange || "--"],
    ["板块", d.board || "--"],
    ["行业", d.industry || "--"],
    ["今开", <span className={s.mono}>{fmtPrice(d.open)}</span>],
    ["最高", <span className={s.mono}>{fmtPrice(d.high)}</span>],
    ["最低", <span className={s.mono}>{fmtPrice(d.low)}</span>],
    ["昨收", <span className={s.mono}>{fmtPrice(d.pre_close)}</span>],
    ["均价", <span className={s.mono}>{fmtPrice(d.avg_price)}</span>],
    [
      "涨停价",
      <span className={s.mono} style={{ color: "var(--up)" }}>
        {fmtPrice(d.high_limit)}
      </span>,
    ],
    [
      "跌停价",
      <span className={s.mono} style={{ color: "var(--down)" }}>
        {fmtPrice(d.low_limit)}
      </span>,
    ],
    ["振幅", <span className={s.mono}>{pct(d.amplitude)}</span>],
    ["换手率", <span className={s.mono}>{pct(d.turnover_rate)}</span>],
    ["量比", <span className={s.mono}>{fmtPrice(d.volume_ratio)}</span>],
    ["市盈(TTM)", <span className={s.mono}>{fmtPrice(d.pe_ttm)}</span>],
    ["市净率", <span className={s.mono}>{fmtPrice(d.pb)}</span>],
    ["成交额", <span className={s.mono}>{fmtAmount(d.amount)}</span>],
    ["流通市值", <span className={s.mono}>{fmtAmount(d.circ_mv)}</span>],
    ["总市值", <span className={s.mono}>{fmtAmount(d.total_mv)}</span>],
    [
      "数据源",
      <span className={s.chipRow}>
        {d.source ? <Badge tone="neutral">{d.source}</Badge> : "--"}
        {/* 行情派生字段与详情源不同源时必须**说清楚**：否则「数据源 eltdx、
            市值却是腾讯来的」会被当成 eltdx 的数据。 */}
        {d.metrics_source && d.metrics_source !== d.source && (
          <Badge tone="neutral">行情 · {d.metrics_source}</Badge>
        )}
      </span>,
    ],
  ];

  return (
    <>
      {d.note && <div className={s.panelNote}>{d.note}</div>}
      <div
        className={s.kvGrid}
        style={maxBodyHeight ? { maxHeight: maxBodyHeight, overflow: "auto" } : undefined}
      >
        {rows.map(([k, v]) => (
          <div key={k} className={s.kvCell}>
            <span className={s.kvCellKey}>{k}</span>
            <span className={s.kvCellVal}>{v}</span>
          </div>
        ))}
        {/* 概念标签数量不定，独占整行（挤在半栏里会换行成一团） */}
        <div className={s.kvCell} style={{ gridColumn: "1 / -1" }}>
          <span className={s.kvCellKey}>概念</span>
          <span className={s.kvCellVal}>
            {d.concepts && d.concepts.length ? (
              <span className={s.chipRow}>
                {d.concepts.slice(0, 8).map((c) => (
                  <Badge key={c} tone="neutral">
                    {c}
                  </Badge>
                ))}
              </span>
            ) : (
              "--"
            )}
          </span>
        </div>
      </div>
    </>
  );
}

export default StockInfoPanel;
