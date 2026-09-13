import { useEffect } from "react";
import { acquireQuotes, releaseQuotes } from "@/stores/quotes";

/**
 * 订阅一组标的的实时行情。
 *
 * 关键：多个组件订阅同一标的时，store 内部以引用计数聚合，
 * 只向服务端下发一次 subscribe。这修掉了旧前端
 * 「每个 Tab 各自 useQuotes → 最多 24 个 WS 订阅」的浪费。
 *
 * 用法：
 *   const codes = useMemo(() => ["000001.SZ"], []);
 *   useQuoteSubscription(codes);
 */
export function useQuoteSubscription(codes: string[]): void {
  const key = codes.join(",");
  useEffect(() => {
    if (codes.length === 0) return;
    acquireQuotes(codes);
    return () => releaseQuotes(codes);
    // key 作为稳定依赖，避免数组字面量导致反复订阅
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}
