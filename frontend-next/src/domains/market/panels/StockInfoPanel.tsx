import { Badge, EmptyState, Spinner } from "@/design/primitives";
import { marketApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtPrice } from "@/shared/format";
import s from "./panels.module.css";

/**
 * 股票基本信息面板（工作台底部坞的「基本信息」页签）。
 *
 * ★ 契约要点（`market.py:market_stock_info`）：
 *   - `GET /market/stock-info?code=&conn_id=&source=` → 名称 / 交易所 / 板块 /
 *     涨跌停 / 昨收 / 行业 / 概念 / source
 *   - 代码会被后端补交易所后缀；**裸代码查不到中文名**（界面只剩数字）
 *   - 全部源不可用时返回 `note` 字段说明「板块是按代码前缀推断的」——
 *     这种降级必须显示给用户，否则「推断值」会被当成事实
 *   - `high_limit` / `low_limit` / `pre_close` 可能为 `null`：显示「—」，不显示 0
 */
export interface StockInfoPanelProps {
  code: string;
}

export function StockInfoPanel({ code }: StockInfoPanelProps) {
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
    ["昨收", <span className={s.mono}>{fmtPrice(d.pre_close)}</span>],
    ["行业", d.industry || "--"],
    [
      "概念",
      d.concepts && d.concepts.length ? (
        <span className={s.chipRow}>
          {d.concepts.slice(0, 8).map((c) => (
            <Badge key={c} tone="neutral">
              {c}
            </Badge>
          ))}
        </span>
      ) : (
        "--"
      ),
    ],
    ["数据源", d.source ? <Badge tone="neutral">{d.source}</Badge> : "--"],
  ];

  return (
    <>
      {d.note && <div className={s.panelNote}>{d.note}</div>}
      <div className={s.kvGrid}>
        {rows.map(([k, v]) => (
          <div key={k} className={s.kvCell}>
            <span className={s.kvCellKey}>{k}</span>
            <span className={s.kvCellVal}>{v}</span>
          </div>
        ))}
      </div>
    </>
  );
}

export default StockInfoPanel;
