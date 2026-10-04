import { useEffect, useRef } from "react";
import { useUiStore } from "@/stores/ui";
import { resolveCssVars } from "./cssVar";
import { ensureECharts, type EChartsInstance, type EChartsOption } from "./echartsSetup";
import s from "./charts.module.css";

/**
 * ECharts 容器。
 *
 * 与旧前端 Chart.jsx 的差异（旧实现每次 setOption(option, true) 全量重建）：
 * - 增量更新：默认 notMerge=false，数据变化走 merge 而非重建
 * - 实例复用：同一容器只 init 一次，option 变化时复用实例
 * - resize：ResizeObserver 单一来源（旧实现同时挂 window resize + RO，重复触发）
 *
 * ## ★ 令牌配色（2026-10-03 修静默失效）
 * ECharts 走 **CanvasRenderer**，而 Canvas **不解析 CSS 自定义属性**：
 * 把 `var(--up)` 写进 option，zrender 的颜色解析器拿到 `undefined`，图表照画、
 * 颜色却是兜底色 —— 于是「切到红涨蓝跌后，K 线（klinecharts，真取令牌）变蓝了，
 * 而分时 / 资金流 / 市场结构 / 板块雷达纹丝不动」。
 *
 * 修法在**这一层**统一做（见 `charts/cssVar.ts` 的说明）：`setOption` 之前用
 * `resolveCssVars` 把 option 里的 `var(--x)` 换成 `getComputedStyle` 取到的真值。
 * 调用方因此**不必**改字符串，也不必各自实现一遍解析 —— 不会出现「改了 3 个、
 * 漏了第 4 个」的漂移。
 *
 * ## ★ 主题 / 涨跌配色变化必须重刷
 * 色值是在 `setOption` 那一刻**快照**进 option 的，之后 `<html data-theme>` /
 * `data-updown` 变了，画布不会自己重取。故这里订阅 `theme` / `updown`，
 * 变化时用**当前 option**重新解析 + `setOption`（不 dispose 实例，缩放不复位）。
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
  /** 令牌真值的**唯一触发源**：主题 / 涨跌配色变化 ⇒ 重新解析 option 并重绘 */
  const uiTheme = useUiStore((st) => st.theme);
  const updown = useUiStore((st) => st.updown);

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
    // ★ 解析放在 setOption 之前：canvas 不认 var()，交给 cssVar 取真值
    inst.setOption(resolveCssVars(option), { notMerge, lazyUpdate: true });
    // ★ theme/updown 进依赖：换肤 / 换涨跌口径后用同一份 option 重新解析一次
  }, [option, notMerge, uiTheme, updown]);

  return <div className={[s.wrap, className ?? ""].filter(Boolean).join(" ")} ref={elRef} />;
}
