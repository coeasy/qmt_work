import type { ReactNode } from "react";
import s from "./primitives.module.css";

export type BadgeTone =
  | "neutral"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "up"
  | "down";

export interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
  title?: string;
}

const TONE_CLASS: Record<BadgeTone, string> = {
  neutral: "",
  success: s.badgeSuccess ?? "",
  warning: s.badgeWarning ?? "",
  danger: s.badgeDanger ?? "",
  info: s.badgeInfo ?? "",
  up: s.badgeUp ?? "",
  down: s.badgeDown ?? "",
};

export function Badge({ tone = "neutral", children, className, title }: BadgeProps) {
  const cls = [s.badge, TONE_CLASS[tone], className ?? ""].filter(Boolean).join(" ");
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

/** 连接状态徽标：券商连接状态在各处复用，统一口径 */
export function StatusBadge({ connected, label }: { connected: boolean; label?: string }) {
  return (
    <Badge tone={connected ? "success" : "danger"}>
      {label ?? (connected ? "已连接" : "未连接")}
    </Badge>
  );
}
