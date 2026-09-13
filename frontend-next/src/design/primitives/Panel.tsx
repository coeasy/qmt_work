import type { ReactNode } from "react";
import s from "./primitives.module.css";

export interface PanelProps {
  title?: ReactNode;
  /** 标题右侧的操作区（按钮/筛选等） */
  extra?: ReactNode;
  children: ReactNode;
  /** 内容区是否去掉内边距（表格/图表类页面用） */
  flush?: boolean;
  padded?: boolean;
  className?: string;
  bodyClassName?: string;
}

export function Panel({
  title,
  extra,
  children,
  flush = false,
  padded = false,
  className,
  bodyClassName,
}: PanelProps) {
  const bodyCls = [
    flush ? s.panelBodyFlush : s.panelBody,
    padded ? s.panelPad : "",
    bodyClassName ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={[s.panel, className ?? ""].filter(Boolean).join(" ")}>
      {(title !== undefined || extra !== undefined) && (
        <div className={s.panelHead}>
          {title !== undefined && <span className={s.panelTitle}>{title}</span>}
          <span className={s.panelSpacer} />
          {extra}
        </div>
      )}
      <div className={bodyCls}>{children}</div>
    </div>
  );
}

export function EmptyState({
  text,
  actionText,
  onAction,
}: {
  text: string;
  actionText?: string;
  onAction?: () => void;
}) {
  return (
    <div className={s.empty}>
      <span>{text}</span>
      {actionText !== undefined && onAction !== undefined && (
        <button type="button" className={s.emptyAction} onClick={onAction}>
          {actionText}
        </button>
      )}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className={s.empty}>
      <span className={s.spinner} />
      {label !== undefined && <span>{label}</span>}
    </div>
  );
}
