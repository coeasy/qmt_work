import { useRef, type CSSProperties, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import s from "./DataTable.module.css";

export interface Column<T> {
  key: string;
  header: string;
  /** 数字宽度或 CSS 宽度；不传则 flex:1 均分 */
  width?: number;
  align?: "left" | "right" | "center";
  mono?: boolean;
  render: (row: T, index: number) => ReactNode;
}

export type Tone = -1 | 0 | 1;

export interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
  rowHeight?: number;
  onRowClick?: (row: T) => void;
  activeKey?: string;
  emptyText?: string;
  /** 涨跌着色：-1 跌 / 0 平 / 1 涨。仅作用于未显式着色的单元格 */
  rowTone?: (row: T) => Tone;
}

const TONE_CLASS: Record<string, string> = {
  "1": s.toneUp ?? "",
  "-1": s.toneDown ?? "",
  "0": s.toneFlat ?? "",
};

/**
 * 虚拟滚动表格。
 *
 * 性能设计：固定行高 + useVirtualizer 只渲染可视区行，支撑十万级行数。
 * 行情高频更新时，配合 Zustand selector 按 code 订阅，可避免整表重渲。
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  rowHeight = 22,
  onRowClick,
  activeKey,
  emptyText = "暂无数据",
  rowTone,
}: DataTableProps<T>) {
  const bodyRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => bodyRef.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  });

  const cellStyle = (c: Column<T>): CSSProperties =>
    c.width !== undefined
      ? { width: c.width, flex: "0 0 auto" }
      : { flex: "1 1 0", minWidth: 0 };

  const alignCls = (c: Column<T>): string =>
    c.align === "right" ? (s.cellRight ?? "") : c.align === "center" ? (s.cellCenter ?? "") : "";

  return (
    <div className={s.wrap}>
      <div className={s.head} role="row">
        {columns.map((c) => (
          <div
            key={c.key}
            role="columnheader"
            className={[s.headCell, alignCls(c)].filter(Boolean).join(" ")}
            style={cellStyle(c)}
          >
            {c.header}
          </div>
        ))}
      </div>

      <div className={s.body} ref={bodyRef}>
        {rows.length === 0 ? (
          <div className={s.empty}>{emptyText}</div>
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((v) => {
              const row = rows[v.index];
              if (row === undefined) return null;
              const key = rowKey(row, v.index);
              const tone = rowTone ? rowTone(row) : 0;
              return (
                <div
                  key={key}
                  role="row"
                  className={[
                    s.row,
                    onRowClick ? s.rowInteractive : "",
                    activeKey === key ? s.rowActive : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  style={{ height: v.size, transform: `translateY(${v.start}px)` }}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                >
                  {columns.map((c) => (
                    <div
                      key={c.key}
                      role="cell"
                      className={[
                        s.cell,
                        alignCls(c),
                        c.mono ? s.cellMono : "",
                        TONE_CLASS[String(tone)] ?? "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      style={cellStyle(c)}
                    >
                      {c.render(row, v.index)}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
