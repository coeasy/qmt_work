import { useMemo } from "react";
import { EmptyState, Spinner } from "@/design/primitives";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "@/charts/echartsSetup";
import { marketApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { fmtPct, fmtPrice } from "@/shared/format";
import d from "../../domain.module.css";
import s from "./panels.module.css";

/**
 * 分时图面板（1 分钟价格线 + 均价线 + 分钟量）。
 *
 * 从 `Minutes.tsx` 抽出：行情工作台与「分时图」页共用**同一份实现** ——
 * 同一个图表在两处各写一套配置，改了一处漏另一处，正是合并展示要消除的隐患。
 *
 * ★ 契约要点（market.py:market_minutes → hub.get_minutes）：
 *   - 返回 {code, trading_date, pre_close, open_price, points:[{t,price,avg,volume}]}
 *   - 数据源只有 TDX 公共行情（券商 SDK 无分时接口）；全源无数据返回 503
 *   - 分时不是 K 线周期：走 /market/minutes，**不能**用 /market/kline 的 tick 周期
 *     （后端会显式 400 并指向本端点，避免静默返回 0 根导致图表空白无报错）
 *
 * 价格线以昨收为基准着色（涨红跌绿，随 --up/--down 令牌变化）。
 * 零 mock：无数据即显式提示，不补假值。
 */
export function MinutesChart({
  code,
  date = "",
  reloadToken = 0,
}: {
  code: string;
  date?: string;
  /** 外部「刷新」按钮用：自增即重新取数（面板自己不放刷新按钮，避免两处各一个） */
  reloadToken?: number;
}) {
  const quote = useQuotesStore((st) => st.quotes[code]);
  const res = useAsync(() => marketApi.minutes(code, date), [code, date, reloadToken]);
  const data = res.data;
  const points = data?.points ?? [];

  const preClose = data?.pre_close ?? quote?.pre_close ?? 0;
  const last = points.length > 0 ? points[points.length - 1] : undefined;
  const changePct = last && preClose ? ((last.price - preClose) / preClose) * 100 : undefined;

  const option = useMemo<EChartsOption>(() => {
    const times = points.map((p) => p.t);
    const prices = points.map((p) => p.price);
    const avgs = points.map((p) => p.avg ?? null);
    const vols = points.map((p) => p.volume ?? 0);
    const lineColor = changePct === undefined ? "#888" : changePct >= 0 ? "#ef4444" : "#22c55e";

    return {
      animation: false,
      backgroundColor: "transparent",
      grid: [
        { left: 56, right: 16, top: 16, height: "58%" },
        { left: 56, right: 16, top: "76%", height: "18%" },
      ],
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      xAxis: [
        {
          type: "category",
          data: times,
          gridIndex: 0,
          boundaryGap: false,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: "var(--chart-grid)" } },
        },
        {
          type: "category",
          data: times,
          gridIndex: 1,
          boundaryGap: false,
          axisLabel: { color: "var(--chart-axis)", fontSize: 10, interval: 29 },
          axisLine: { lineStyle: { color: "var(--chart-grid)" } },
        },
      ],
      yAxis: [
        {
          type: "value",
          gridIndex: 0,
          scale: true,
          axisLabel: { color: "var(--chart-axis)", fontSize: 10 },
          splitLine: { lineStyle: { color: "var(--chart-grid)" } },
        },
        {
          type: "value",
          gridIndex: 1,
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: "价格",
          type: "line",
          data: prices,
          xAxisIndex: 0,
          yAxisIndex: 0,
          showSymbol: false,
          lineStyle: { width: 1.4, color: lineColor },
          markLine: preClose
            ? {
                silent: true,
                symbol: "none",
                label: { formatter: "昨收", fontSize: 10, color: "var(--chart-axis)" },
                lineStyle: { type: "dashed", color: "var(--chart-crosshair)" },
                data: [{ yAxis: preClose }],
              }
            : undefined,
        },
        {
          name: "均价",
          type: "line",
          data: avgs,
          xAxisIndex: 0,
          yAxisIndex: 0,
          showSymbol: false,
          lineStyle: { width: 1, type: "dashed", color: "#f59e0b" },
        },
        {
          name: "分钟量",
          type: "bar",
          data: vols,
          xAxisIndex: 1,
          yAxisIndex: 1,
          itemStyle: { color: "var(--chart-ma10)" },
        },
      ],
    };
  }, [points, preClose, changePct]);

  return (
    <div className={s.fill}>
      {data && (
        <div className={s.miniBar}>
          <span className={d.muted}>{data.trading_date}</span>
          <span className={d.mono}>
            {fmtPrice(last?.price)} {fmtPct(changePct)}
          </span>
          <span className={d.muted}>昨收 {fmtPrice(preClose)}</span>
        </div>
      )}

      {res.error && (
        <div className={`${d.note} ${d.noteWarn}`} style={{ margin: "0 8px 6px" }}>
          {res.error}
          {res.error.includes("分时") &&
            " —— 分时依赖 TDX 公共行情源，非交易时段或网络不可用时无数据（零 mock，不补假值）。"}
        </div>
      )}

      <div className={s.chartBody}>
        {res.loading && points.length === 0 ? (
          <Spinner label="加载分时…" />
        ) : points.length === 0 ? (
          <EmptyState text="暂无分时数据" />
        ) : (
          <EChart option={option} />
        )}
      </div>
    </div>
  );
}

export default MinutesChart;
