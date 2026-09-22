import {
  MAIN_INDICATORS,
  MAX_SUB_INDICATORS,
  SUB_INDICATORS,
  type IndicatorDef,
} from "@/shared/indicators";
import s from "./charts.module.css";

/**
 * K 线指标选择条 —— 主图叠加 + 副图，点击即切换（多选）。
 *
 * ★ 为什么做成常驻 chip 条而不是下拉菜单：指标是看盘时**高频切换**的东西
 *   （看两眼 MACD 又切回成交量），下拉要两步、且选完看不见「当前开了哪几个」。
 *   chip 条把选中态直接画在界面上，一眼可数。
 *
 * ★ 副图有数量上限（`MAX_SUB_INDICATORS`）：副图 pane 平分图表高度，开太多
 *   会把主图压成一条缝。达到上限时**明确提示**而不是默默失效。
 */

export interface IndicatorPickerProps {
  main: readonly string[];
  sub: readonly string[];
  onChange: (main: string[], sub: string[]) => void;
  /** 达到上限等提示的落点（不传则不提示） */
  onNotice?: (text: string) => void;
}

function Chip({
  def,
  on,
  onClick,
}: {
  def: IndicatorDef;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={on ? `${s.indChip} ${s.indChipOn}` : s.indChip}
      onClick={onClick}
      title={def.hint}
      data-on={on ? "1" : "0"}
    >
      {def.label}
    </button>
  );
}

export function IndicatorPicker({ main, sub, onChange, onNotice }: IndicatorPickerProps) {
  const toggle = (kind: "main" | "sub", id: string) => {
    if (kind === "main") {
      const next = main.includes(id) ? main.filter((x) => x !== id) : [...main, id];
      onChange(next, [...sub]);
      return;
    }
    if (sub.includes(id)) {
      onChange([...main], sub.filter((x) => x !== id));
      return;
    }
    if (sub.length >= MAX_SUB_INDICATORS) {
      // 静默失效会被读成「软件没反应」，必须说清楚为什么不生效
      onNotice?.(`副图最多同时叠加 ${MAX_SUB_INDICATORS} 个，请先取消一个再添加`);
      return;
    }
    onChange([...main], [...sub, id]);
  };

  return (
    <div className={s.indBar}>
      <span className={s.indGroup}>主图</span>
      {MAIN_INDICATORS.map((d) => (
        <Chip key={d.id} def={d} on={main.includes(d.id)} onClick={() => toggle("main", d.id)} />
      ))}
      <span className={s.indSep} />
      <span className={s.indGroup}>副图</span>
      {SUB_INDICATORS.map((d) => (
        <Chip key={d.id} def={d} on={sub.includes(d.id)} onClick={() => toggle("sub", d.id)} />
      ))}
    </div>
  );
}

export default IndicatorPicker;
