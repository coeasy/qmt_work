import { useMemo } from "react";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "./useQuoteSubscription";
import type { Quote } from "@/shared/types";

/**
 * 一次拿到「一组标的」的实时行情：**订阅 + 取值** 的唯一入口。
 *
 * ## 为什么必须抽这一层
 *
 * 原来每个页面都得自己写两行：
 * ```ts
 * useQuoteSubscription(useMemo(() => codes, [codes]));
 * const quotes = useQuotesStore((st) => st.quotes);
 * ```
 * 这两行看着简单，但**漏掉第一行不会报错**，表现只是「列表里的价格永远是 `--`」
 * —— 这正是「很多界面没有真实数据」里**最隐蔽**的一类成因：不红不报，
 * 只有用户盯着屏幕才发现数字不动。
 *
 * 抽成一个 hook 后调用方只有一种写法，想漏也漏不掉。
 *
 * ## 为什么返回「按 code 索引的映射」而不是整个 quotes
 *
 * 调用方（表格/列表）要的是「我这几行各自的最新价」；直接给整个 store 的
 * `quotes` 会把无关标的也带进来，列渲染时还要自己再查一次。
 */
export function useLiveQuotes(codes: string[]): Record<string, Quote | undefined> {
  // 依赖用「逗号拼接后的字符串」，避免调用方传字面量数组导致每次渲染都重新订阅
  const key = useMemo(() => codes.join(","), [codes]);
  const stable = useMemo(() => (key ? key.split(",") : []), [key]);
  useQuoteSubscription(stable);
  const quotes = useQuotesStore((st) => st.quotes);
  return useMemo(() => {
    const out: Record<string, Quote | undefined> = {};
    for (const c of stable) out[c] = quotes[c];
    return out;
  }, [stable, quotes]);
}

/**
 * 取「一组标的」的最新价，缺失时回退到 fallback（券商快照值）。
 *
 * 用途：持仓/委托这类列表，后端**只在查询那一刻**给一次现价（`position.price`），
 * 之后不会自己更新。行情订阅可用时用实时价，不可用时（无券商连接 / 未订阅到）
 * 如实回退快照 —— 两者都不是 0，别拿 0 冒充「没数据」。
 */
export function useLivePrices(
  codes: string[],
  fallback: Record<string, number | undefined>,
): Record<string, number | undefined> {
  const quotes = useLiveQuotes(codes);
  return useMemo(() => {
    const out: Record<string, number | undefined> = {};
    for (const c of codes) {
      const q = quotes[c];
      out[c] = q !== undefined && q.price > 0 ? q.price : fallback[c];
    }
    return out;
  }, [codes, quotes, fallback]);
}
