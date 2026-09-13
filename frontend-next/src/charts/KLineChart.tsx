import { useEffect, useRef } from "react";
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
  /** 主图叠加指标，默认 MA */
  indicators?: string[];
  /** 副图指标 */
  subIndicators?: string[];
  /** 是否显示左上角最新价读数 */
  showReadout?: boolean;
}

export function KLineChart({
  code,
  period,
  count = 500,
  linkGroupName,
  indicators = ["MA"],
  subIndicators = ["VOL"],
  showReadout = true,
}: KLineChartProps) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const linkHandlerRef = useRef<((p: CrosshairPayload) => void) | null>(null);

  const quote = useQuotesStore((st) => st.quotes[code]);

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
          .kline(code, period, count)
          .then((res) => {
            // 契约：/market/kline 返回 { bars: [...], stale, source, ... }，不是裸数组
            // toKLineData 对无法解析的时间返回 null（见其注释），此处过滤掉
            let data = (res.bars ?? [])
              .map(toKLineData)
              .filter((d): d is KLineData => d !== null);
            if (type === "forward" && typeof timestamp === "number") {
              data = data.filter((d) => d.timestamp < timestamp);
            }
            // 后端不支持日期分页，向前翻页不继续请求，避免重复拉取同一窗口
            callback(data, false);
          })
          .catch(() => callback([], false));
      },
    });

    for (const ind of indicators) chart.createIndicator(ind, false);
    for (const ind of subIndicators) chart.createIndicator(ind, false);

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
  }, [code, period, count, linkGroupName, indicators, subIndicators]);

  const price = quote?.price;
  const changePct = quote?.change_pct;

  return (
    <div className={s.wrap}>
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
    </div>
  );
}
