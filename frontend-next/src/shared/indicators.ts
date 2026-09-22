/**
 * K 线可叠加指标清单（**纯数据，不 import 图表库**）。
 *
 * ★ 为什么单独放在 `shared/` 而不是 `charts/KLineChart.tsx`：KLineChart 会
 *   `import "klinecharts"`（体积不小），而指标名称只是数据 —— 引用清单的组件
 *   不该被顺带拖进整个图表库。与 `shared/periods.ts` 同一个理由。
 */

export interface IndicatorDef {
  /** klinecharts 内置指标名，直接传给 `createIndicator` */
  id: string;
  label: string;
  hint: string;
}

/** 主图叠加指标：画在 K 线之上（同一 pane，可多选叠加） */
export const MAIN_INDICATORS: readonly IndicatorDef[] = [
  { id: "MA", label: "MA", hint: "均线（5/10/20/60）" },
  { id: "EMA", label: "EMA", hint: "指数平滑均线" },
  { id: "BOLL", label: "BOLL", hint: "布林带（中轨 ± 2 倍标准差）" },
  { id: "SAR", label: "SAR", hint: "抛物线转向" },
  { id: "BBI", label: "BBI", hint: "多空均线" },
];

/** 副图指标：各自占一个 pane（可多选） */
export const SUB_INDICATORS: readonly IndicatorDef[] = [
  { id: "VOL", label: "VOL", hint: "成交量" },
  { id: "MACD", label: "MACD", hint: "指数平滑异同平均" },
  { id: "KDJ", label: "KDJ", hint: "随机指标" },
  { id: "RSI", label: "RSI", hint: "相对强弱" },
  { id: "WR", label: "WR", hint: "威廉指标" },
  { id: "BIAS", label: "BIAS", hint: "乖离率" },
  { id: "CCI", label: "CCI", hint: "顺势指标" },
  { id: "ATR", label: "ATR", hint: "真实波幅" },
  { id: "OBV", label: "OBV", hint: "能量潮" },
  { id: "DMI", label: "DMI", hint: "趋向指标" },
];

/**
 * 副图最多同时开几个。
 *
 * ★ 上限不是拍脑袋：副图 pane 会**平分**图表高度，开 5 个以后每个 pane 只剩
 *   几十像素，K 线主图被压成一条缝 —— 那时候「叠加了指标」反而是负优化。
 *   超出时前端直接拒绝并给出提示，而不是让用户自己发现图被压扁。
 */
export const MAX_SUB_INDICATORS = 4;
