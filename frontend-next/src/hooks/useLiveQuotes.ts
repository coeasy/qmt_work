import { useEffect, useMemo, useState } from "react";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "./useQuoteSubscription";
import { isLivePrice } from "@/shared/freshness";
import { marketApi } from "@/services/api/market";
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
      // 判据统一走 shared/freshness.ts::isLivePrice（0 与缺失都算「没行情」）
      const q = quotes[c]?.price;
      out[c] = isLivePrice(q) ? q : fallback[c];
    }
    return out;
  }, [codes, quotes, fallback]);
}

/**
 * 兜底快照：从 `/market/quotes` 拉一次**最近交易日收盘**行情。
 *
 * ## 为什么需要（2026-09-20 实测）
 * `useLiveQuotes` 只走 WS 订阅，而 WS 是**券商实时推送**：
 * **休市 / 节假日 / 盘后 / 没连券商**时一条都不推。于是自选股一片 `--`，
 * 用户会读成「软件坏了」或「这票退市了」—— 真实情况只是今天不开市。
 * 界面应该显示**最近交易日的收盘价**（顶部 `TradingDateBadge` 已标了日期），
 * 而不是留空。
 *
 * ## 为什么不放在 useLiveQuotes 里
 * 持仓/委托那些列表**不需要**这层兜底（它们的价格来自后端查询快照，
 * 走 `useLivePrices(fallback)` 那条路）。混进去会让每次订阅都多发一次 REST。
 * 只有「看板类」列表（自选股/报价牌）需要 —— 用下面的 `useDisplayQuotes` 组合。
 *
 * ⚠️ 拿不到就返回空表，绝不写 0（`fmtPrice(undefined)` 才是诚实的 `--`）。
 */
export function useFallbackQuotes(codes: string[]): Record<string, Quote | undefined> {
  const key = useMemo(() => codes.join(","), [codes]);
  const stable = useMemo(() => (key ? key.split(",") : []), [key]);
  const [snap, setSnap] = useState<Record<string, Quote>>({});

  useEffect(() => {
    if (stable.length === 0) {
      setSnap({});
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    /** 已拿到的兜底数量 —— 空响应不覆盖已有数据，且触发快速重试 */
    let got = 0;

    /**
     * 单次拉取。返回「本次拿到的条数」，交给调用方决定要不要快重试。
     *
     * ★ 为什么必须区分「空响应」：后端 tencent 公共源有**全局节流**
     *   （`_PublicSource._MIN_INTERVAL=0.3s`，跨请求共享一把锁），冷启动时
     *   首次拉取经常在 5s 超时窗口内拿不到任何东西。若一律等到 15s 的
     *   轮询才重试，用户会盯着一片 `--` 发呆 —— 所以空响应走**快速重试**。
     */
    const fetchOnce = (): Promise<number> =>
      marketApi
        .quotes(stable)
        .then((resp) => {
          if (cancelled) return 0;
          const items = resp?.items ?? [];
          const out: Record<string, Quote> = {};
          for (const r of items) {
            // 「名 == 代码」= 后端没查到真名 ⇒ 置空（渲染时回退到代码，不冒充）
            if (!r?.code) continue;
            out[r.code] = r.name && r.name !== r.code ? r : { ...r, name: "" };
          }
          const n = Object.keys(out).length;
          // 空响应不覆盖已有数据（避免「有 → 空」的闪烁）
          if (n > 0) {
            setSnap(out);
            got = n;
          }
          return n;
        })
        .catch(() => 0);

    // ① 立即拉一次（满足「打开应用就有数字」的诉求）
    fetchOnce().then((n) => {
      // ② 首次为空 ⇒ 1s 后快重试（cold start 时后端打源还没热）
      if (!cancelled && n === 0) {
        timer = setTimeout(() => {
          if (cancelled) return;
          fetchOnce().then((m) => {
            // ③ 还是空 ⇒ 交给 15s 的常规轮询继续兜
            if (!cancelled && m === 0) {
              timer = setInterval(() => {
                if (!cancelled) void fetchOnce();
              }, 15_000);
            }
          });
        }, 1000);
      }
    });

    return () => {
      cancelled = true;
      if (timer !== null) {
        clearTimeout(timer);
        clearInterval(timer);
      }
      void got;
    };
  }, [key]);

  return useMemo(() => {
    const out: Record<string, Quote | undefined> = {};
    for (const c of stable) out[c] = snap[c];
    return out;
  }, [stable, snap]);
}

/**
 * ★ 展示用行情：**WS 实时优先，缺失时回退最近交易日收盘**。
 *
 * 「看板类」列表（自选股 / 报价牌 / 侧栏自选面板）应该一律用这个，而不是
 * 直接用 `useLiveQuotes` —— 后者在休市时满屏 `--`。
 *
 * ## 合并规则（双源不打架）
 * - **WS 有实时价** ⇒ 用它（开市时唯一可信来源）；名称缺失再用兜底名补齐；
 * - **WS 无实时价** ⇒ 用最近交易日收盘快照（休市/未连券商时的正常展示）；
 * - **两边都没有** ⇒ `undefined`，渲染成 `--`。
 *
 * 判据统一走 `shared/freshness.ts::isLivePrice`（`0` 与缺失都算「没行情」）——
 * 别拿 0 冒充收盘价。
 */
export function useDisplayQuotes(codes: string[]): Record<string, Quote | undefined> {
  const live = useLiveQuotes(codes);
  const fb = useFallbackQuotes(codes);
  return useMemo(() => {
    const out: Record<string, Quote | undefined> = {};
    for (const c of codes) {
      const l = live[c];
      const f = fb[c];
      if (l && isLivePrice(l.price)) {
        out[c] = { ...l, name: l.name || f?.name || "" };
      } else if (f) {
        out[c] = f;
      } else {
        out[c] = l; // 没价但有别的字段（如名称）时也带出来，绝不丢信息
      }
    }
    return out;
  }, [codes, live, fb]);
}
