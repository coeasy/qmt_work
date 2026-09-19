import { useEffect } from "react";
import { Badge } from "./Badge";
import { useSessionStore, sessionDateInfo } from "@/stores/session";
import { fmtBarDate } from "@/services/api";

/**
 * 交易日期角标 —— 「你现在看的是哪一天的数据」。
 *
 * 为什么必须有：非交易日（周末/节假日）后端各接口照常返回**上一交易日**的数据，
 * 这是正确的；但界面此前没有任何一处标注这一点，用户会把周六看到的数字当成
 * 「今天的行情」。这个角标把「今日休市 · 最近交易日 20260918」明说出来。
 *
 * 规则（见 `stores/session.ts::sessionDateInfo`）：
 * - 交易日 → 「今日 · 20260919 交易中/已收盘」
 * - 非交易日 → 「最近交易日 · 20260918 已收盘」
 *
 * 数据取不到时**不渲染**（宁可不显示，也不显示一个可能错的日期）。
 */
export interface TradingDateBadgeProps {
  /** 额外的自定义类名（由调用方控制布局位置） */
  className?: string;
  /** 是否在挂载时自动拉取（默认 true；同一页面多处使用时只让第一处拉） */
  autoFetch?: boolean;
}

export function TradingDateBadge({ className, autoFetch = true }: TradingDateBadgeProps) {
  const snapshot = useSessionStore((st) => st.snapshot);
  const refreshSnapshot = useSessionStore((st) => st.refreshSnapshot);

  useEffect(() => {
    if (!autoFetch) return;
    void refreshSnapshot();
    // 会话状态会随时间变化（开盘/午休/收盘），但变化频率很低；
    // 5 分钟刷新一次足够，且避免与状态栏的 /health 轮询叠加成无谓请求。
    const timer = window.setInterval(() => void refreshSnapshot(), 5 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [autoFetch, refreshSnapshot]);

  const info = sessionDateInfo(snapshot, fmtBarDate);
  if (!info.label) return null;

  return (
    <Badge tone={info.offDay ? "neutral" : "info"} title={info.title} className={className}>
      {info.label}
    </Badge>
  );
}
