import * as echarts from "echarts/core";
import {
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  TreemapChart,
  HeatmapChart,
} from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  TitleComponent,
  MarkLineComponent,
  VisualMapComponent,
  DatasetComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

/**
 * ECharts 按需注册（单一注册点）。
 *
 * 只注册实际用到的图表与组件，避免整包引入。
 * K 线/分时不在此列 —— 那部分由 klinecharts 负责（见方案 §4.1 的严格分工）。
 */
let registered = false;

export function ensureECharts(): typeof echarts {
  if (!registered) {
    echarts.use([
      BarChart,
      LineChart,
      PieChart,
      ScatterChart,
      TreemapChart,
      HeatmapChart,
      GridComponent,
      TooltipComponent,
      LegendComponent,
      DataZoomComponent,
      TitleComponent,
      MarkLineComponent,
      VisualMapComponent,
      DatasetComponent,
      CanvasRenderer,
    ]);
    registered = true;
  }
  return echarts;
}

export type EChartsInstance = echarts.ECharts;
export type EChartsOption = echarts.EChartsCoreOption;
