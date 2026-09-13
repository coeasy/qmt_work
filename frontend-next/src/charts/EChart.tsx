import { useEffect, useRef } from "react";
import { ensureECharts, type EChartsInstance, type EChartsOption } from "./echartsSetup";
import s from "./charts.module.css";

/**
 * ECharts 容器。
 *
 * 与旧前端 Chart.jsx 的差异（旧实现每次 setOption(option, true) 全量重建）：
 * - 增量更新：默认 notMerge=false，数据变化走 merge 而非重建
 * - 实例复用：同一容器只 init 一次，option 变化时复用实例
 * - resize：ResizeObserver 单一来源（旧实现同时挂 window resize + RO，重复触发）
 * - 主题跟随设计令牌，主题切换时重建实例以应用新配色
 */

export interface EChartProps {
  option: EChartsOption;
  /** true = 完全替换（切换图表类型时用），默认 false 增量合并 */
  notMerge?: boolean;
  theme?: string;
  className?: string;
  onReady?: (inst: EChartsInstance) => void;
}

export function EChart({
  option,
  notMerge = false,
  theme,
  className,
  onReady,
}: EChartProps) {
  const elRef = useRef<HTMLDivElement>(null);
  const instRef = useRef<EChartsInstance | null>(null);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const echarts = ensureECharts();

    const inst = echarts.init(el, theme, { renderer: "canvas" });
    instRef.current = inst;
    onReady?.(inst);

    const ro = new ResizeObserver(() => inst.resize());
    ro.observe(el);

    return () => {
      ro.disconnect();
      inst.dispose();
      instRef.current = null;
    };
    // theme 变化需要重建实例才能生效
  }, [theme, onReady]);

  useEffect(() => {
    const inst = instRef.current;
    if (!inst) return;
    inst.setOption(option, { notMerge, lazyUpdate: true });
  }, [option, notMerge]);

  return <div className={[s.wrap, className ?? ""].filter(Boolean).join(" ")} ref={elRef} />;
}
