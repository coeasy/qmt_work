import { useEffect, useRef, useState } from "react";
import { dispose, init, type Chart, type KLineData } from "klinecharts";
import { marketApi } from "@/services/api";
import { useQuotesStore } from "@/stores/quotes";
import { fmtPct, fmtPrice, toneColor } from "@/shared/format";
import { PERIOD_MAP } from "@/shared/periods";
import { toTimestamp } from "@/shared/time";
import type { Bar, Period } from "@/shared/types";
import { broadcastLink, joinLinkGroup, type CrosshairPayload } from "./linkGroup";
import s from "./charts.module.css";

/**
 * K 线图（klinecharts 10）。
 *
 * API 说明：klinecharts 10 用 DataLoader 模式（setDataLoader/getBars），
 * 不再是 v9 的 applyNewData。此处已按 10.x 实际签名实现。
 *
 * 已知限制：后端 /market/kline 不支持按结束日期分页，只能取最近 count 根，
 * 因此向左滚动不会继续加载更早历史（more 恒为 false）。补历史需后端加 end 参数。
 *
 * 体积约定：周期标签等常量放在 @/shared/periods，避免本文件被 Toolbar 引用时
 * 把 klinecharts 拉进主包。
 */

/**
 * 后端时间串 → 毫秒时间戳。
 *
 * ★ 实现已抽到 @/shared/time：后端 `time` 字段存在**多源格式漂移**
 *   （eltdx 为 `20260817`，akshare 为 `2026-08-17`，optional_sources 为
 *   `2026-08-17 00:00:00`）。早期这里只认 `YYYY-MM-DD`，导致 eltdx 数据
 *   全部解析失败并兜底为 Date.now()，X 轴标签重叠成同一天。
 */
function toKLineData(bar: Bar): KLineData | null {
  const timestamp = toTimestamp(bar.time);
  // 解析不出的时间不进入图表：宁可少一根，也不要所有 K 线堆在同一时刻
  if (!Number.isFinite(timestamp)) return null;
  return {
    timestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume ?? 0,
    turnover: bar.amount ?? 0,
  };
}

/** 默认指标：**必须是模块级常量**。
 *
 * ★ 为什么不能写成参数默认值（V11 R14）：此前签名是
 * `indicators = ["MA"], subIndicators = ["VOL"]` —— 数组字面量**每次 render
 * 都是新引用**，而下方 effect 的依赖数组里包含它们。于是只要组件重渲染一次，
 * effect 就会 cleanup + 重跑，即 `dispose(el)` + `init(el)` + 重新请求数据。
 *
 * 本组件又订阅了行情（`useQuotesStore((st) => st.quotes[code])`），行情每个
 * tick 都让组件重渲染 ⇒ **图表每个 tick 被销毁重建一次**。用户看到的就是
 * 「K 线闪断、指标丢失、十字光标复位、缩放被重置」。
 *
 * 同类问题也存在于调用方：`MarketData.tsx` 传 `?? ["MA"]`、
 * `MarketWorkbench.tsx` 不传参数 —— 都会每次新建数组。故依赖改用
 * 序列化后的字符串 key（见 `indicatorsKey`），彻底与数组引用解耦。
 */
const DEFAULT_INDICATORS: readonly string[] = ["MA"];
const DEFAULT_SUB_INDICATORS: readonly string[] = ["VOL"];

export { DEFAULT_INDICATORS, DEFAULT_SUB_INDICATORS };

/** 图表主题样式：令牌驱动，主题切换时重建 */
function chartStyles(): Record<string, unknown> {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    grid: {
      horizontal: { color: v("--chart-grid", "#212b3b") },
      vertical: { color: v("--chart-grid", "#212b3b") },
    },
    candle: {
      bar: {
        upColor: v("--up", "#ef4444"),
        downColor: v("--down", "#22c55e"),
        upBorderColor: v("--up", "#ef4444"),
        downBorderColor: v("--down", "#22c55e"),
        upWickColor: v("--up", "#ef4444"),
        downWickColor: v("--down", "#22c55e"),
      },
      priceMark: {
        last: {
          upColor: v("--up", "#ef4444"),
          downColor: v("--down", "#22c55e"),
        },
      },
    },
    indicator: {
      lines: [
        { color: v("--chart-ma5", "#f59e0b") },
        { color: v("--chart-ma10", "#38bdf8") },
        { color: v("--chart-ma20", "#a78bfa") },
        { color: v("--chart-ma60", "#22c55e") },
      ],
    },
    xAxis: { axisLine: { color: v("--chart-axis", "#6b7a91") } },
    yAxis: { axisLine: { color: v("--chart-axis", "#6b7a91") } },
    crosshair: {
      horizontal: { line: { color: v("--chart-crosshair", "#9aa8bd") } },
      vertical: { line: { color: v("--chart-crosshair", "#9aa8bd") } },
    },
  };
}

export interface KLineChartProps {
  code: string;
  period: Period;
  count?: number;
  /** 联动组名；同组图表十字光标同步 */
  linkGroupName?: string;
  /** 主图叠加指标，默认 MA（只读：调用方传入的数组不参与依赖判断） */
  indicators?: readonly string[];
  /** 副图指标（只读，同上） */
  subIndicators?: readonly string[];
  /** 是否显示左上角最新价读数 */
  showReadout?: boolean;
  /**
   * 复权方式：'' = 不复权 / 'qfq' = 前复权 / 'hfq' = 后复权。
   *
   * ★ 默认 **qfq**。注意后端 `/market/kline` 回包的 `adjust` 是**实际口径**：
   * 若源降级导致拿不到复权价，后端会如实回报，此处角标以**后端回报为准**
   * （见 `meta.adjust`），避免出现「标着前复权、实际是不复权」。
   */
  adj?: string;
  /** 是否显示右上角「复权 / 数据源 / 数据截止日 / 是否陈旧」角标 */
  showMeta?: boolean;
}

export function KLineChart({
  code,
  period,
  count = 500,
  linkGroupName,
  indicators = DEFAULT_INDICATORS,
  subIndicators = DEFAULT_SUB_INDICATORS,
  showReadout = true,
  adj = "qfq",
  showMeta = true,
}: KLineChartProps) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const linkHandlerRef = useRef<((p: CrosshairPayload) => void) | null>(null);

  const quote = useQuotesStore((st) => st.quotes[code]);
  /**
   * K 线元数据（数据源 / 是否陈旧 / 数据截止日 / **实际复权口径**）：
   * 后端给了就必须让用户看见，不能悄悄丢掉。
   */
  const [meta, setMeta] = useState<{
    stale?: boolean;
    source?: string;
    note?: string;
    asOf?: string;
    adjust?: string;
    /** 最后一根 K 线的时间戳 —— 数据真正的「截至日」 */
    lastBarTs?: number;
  }>({});
  /** 空数据状态：`callback([], false)` 之后画布全空且此前**没有任何提示** */
  const [empty, setEmpty] = useState(false);

  // ★ 依赖稳定化（V11 R14）：用**序列化后的 key** 而非数组引用作为 effect 依赖。
  // 否则调用方每次传入新数组（或使用默认值）都会导致图表被销毁重建。
  const indicatorsKey = indicators.join(",");
  const subIndicatorsKey = subIndicators.join(",");

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;

    const chart = init(el, { locale: "zh-CN", styles: chartStyles() });
    if (!chart) return;
    chartRef.current = chart;

    chart.setSymbol({ ticker: code, pricePrecision: 2, volumePrecision: 0 });
    chart.setPeriod(PERIOD_MAP[period]);

    chart.setDataLoader({
      getBars: ({ type, timestamp, callback }) => {
        void marketApi
          .kline(code, period, count, adj)
          .then((res) => {
            // 契约：/market/kline 返回 { bars: [...], stale, source, ... }，不是裸数组
            // toKLineData 对无法解析的时间返回 null（见其注释），此处过滤掉
            // ★ stale / source / note 必须留下来告诉用户：此前整包丢弃，
            //   于是「数据源已降级、K 线是陈旧的」这件事完全不可见 ——
            //   用户以为在看实时数据，实际在看几天前的缓存。
            // 先把 bars 映射成图库数据（注意：必须在 setMeta 之前算出来，
            // 因为「数据截止日」要靠最后一根 K 线推导）。
            let data = (res.bars ?? [])
              .map(toKLineData)
              .filter((d): d is KLineData => d !== null);
            if (type === "forward" && typeof timestamp === "number") {
              data = data.filter((d) => d.timestamp < timestamp);
            }
            const lastBar = data.length ? data[data.length - 1] : undefined;
            setMeta({
              stale: res.stale,
              source: typeof res.source === "string" ? res.source : undefined,
              note: typeof res.note === "string" ? res.note : undefined,
              // ★ as_of（数据截止日）此前**完全没被渲染**：用户看不到
              //   「这根 K 线是哪天的」，非交易日尤其容易把上一交易日的
              //   数据当成今日空图。后端已回该字段，必须显示。
              asOf: typeof res.as_of === "string" ? res.as_of : undefined,
              // ★ 用**后端回报的实际口径**（不是请求口径）：源降级时两者可能不同，
              //   以请求口径展示会造成「标着前复权、实为不复权」。
              adjust: typeof res.adjust === "string" ? res.adjust : undefined,
              // 最后一根 K 线的时间戳 = 数据真正的「截至日」。比 as_of 可靠：
              // as_of 仅在后端判定 stale 时才给（market.py），正常情况下为 null。
              lastBarTs: lastBar?.timestamp,
            });
            // 空数据必须让用户看见原因，而不是留一块白板（此前 `.overlay`
            // 样式定义了却从未使用，画布全空且零提示）。
            setEmpty(data.length === 0);
            // 后端不支持日期分页，向前翻页不继续请求，避免重复拉取同一窗口
            callback(data, false);
          })
          .catch(() => {
            setEmpty(true);
            callback([], false);
          });
      },
    });

    // 从 key 还原数组：与依赖解耦（数组引用不再参与依赖判断）
    for (const ind of indicatorsKey ? indicatorsKey.split(",").filter(Boolean) : []) {
      chart.createIndicator(ind, false);
    }
    for (const ind of subIndicatorsKey ? subIndicatorsKey.split(",").filter(Boolean) : []) {
      chart.createIndicator(ind, false);
    }

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);

    // 跨窗联动：十字光标同步
    if (linkGroupName) {
      const handler = (p: CrosshairPayload) => {
        if (p.dataIndex < 0) return;
        chart.scrollToDataIndex(p.dataIndex);
      };
      linkHandlerRef.current = handler;
      const leave = joinLinkGroup(linkGroupName, handler);

      const onCrosshair = (raw: unknown) => {
        const data = raw as { dataIndex?: number; kLineData?: KLineData } | null;
        const idx = typeof data?.dataIndex === "number" ? data.dataIndex : -1;
        broadcastLink(
          linkGroupName,
          { dataIndex: idx, timestamp: data?.kLineData?.timestamp },
          handler,
        );
      };
      chart.subscribeAction("onCrosshairChange", onCrosshair);

      return () => {
        chart.unsubscribeAction("onCrosshairChange", onCrosshair);
        leave();
        linkHandlerRef.current = null;
        ro.disconnect();
        dispose(el);
        chartRef.current = null;
      };
    }

    return () => {
      ro.disconnect();
      dispose(el);
      chartRef.current = null;
    };
  }, [code, period, count, adj, linkGroupName, indicatorsKey, subIndicatorsKey]);

  const price = quote?.price;
  const changePct = quote?.change_pct;
  /**
   * 复权角标以**后端回报的实际口径**为准（meta.adjust），请求口径仅作兜底。
   * 这样源降级导致拿不到复权价时，角标会如实显示「不复权」而不是继续标「前复权」。
   */
  const effectiveAdj = meta.adjust ?? adj;
  const adjLabel =
    effectiveAdj === "qfq" ? "前复权" : effectiveAdj === "hfq" ? "后复权" : "不复权";
  /** 数据截止日（YYYYMMDD → YYYY-MM-DD）；后端未给则回退到最后一根 K 线的日期 */
  const asOfLabel = (() => {
    if (meta.asOf) {
      return /^\d{8}$/.test(meta.asOf)
        ? `${meta.asOf.slice(0, 4)}-${meta.asOf.slice(4, 6)}-${meta.asOf.slice(6, 8)}`
        : meta.asOf;
    }
    if (typeof meta.lastBarTs === "number" && Number.isFinite(meta.lastBarTs)) {
      const d = new Date(meta.lastBarTs);
      const p = (n: number) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    }
    return "";
  })();

  return (
    <div className={s.wrap}>
      {showMeta && (
        <div
          className={s.meta}
          title={
            meta.note ||
            `复权：${adjLabel}${meta.source ? ` · 数据源：${meta.source}` : ""}${
              asOfLabel ? ` · 数据截至 ${asOfLabel}` : ""
            }`
          }
        >
          <span>{adjLabel}</span>
          {meta.source && <span>· {meta.source}</span>}
          {/* 数据截止日：非交易日看行情时，这是唯一能区分「今日」与「上一交易日」的信息 */}
          {asOfLabel && <span>· 截至 {asOfLabel}</span>}
          {meta.stale && <span className={s.metaStale}>· 数据陈旧</span>}
        </div>
      )}
      {showReadout && quote && (
        <div className={s.readout}>
          <span className={s.readoutName}>{quote.name ?? code}</span>
          <span className={s.readoutPrice} style={{ color: toneColor(changePct) }}>
            {fmtPrice(price)}
          </span>
          <span style={{ color: toneColor(changePct) }}>{fmtPct(changePct)}</span>
        </div>
      )}
      <div className={s.canvas} ref={elRef} />
      {empty && (
        <div className={s.overlay}>
          {meta.note ? `暂无 K 线数据：${meta.note}` : "暂无 K 线数据（可能停牌或该周期无成交）"}
        </div>
      )}
    </div>
  );
}
