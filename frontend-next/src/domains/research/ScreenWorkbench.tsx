import { useState } from "react";
import { Tabs } from "@/design/primitives";
import type { PageProps } from "@/app/routes";
import { ClassicPanel, ConditionsPanel, FormulaPanel } from "./screen/ScreenPanels";
import s from "../domain.module.css";

/**
 * 选股工作台 —— 把「条件选股 / 公式选股 / 经典策略」合并到一个界面。
 *
 * 为什么合并：这三页问的是同一个问题（「按我的规则筛出哪些票」），区别只是规则的
 * 写法 —— 条件树 JSON、类通达信公式、还是现成的经典形态策略。后端也共用同一套
 * 「解析股票池 → 批量取 K 线 → 求值 → 溯源」编排（app/screener/engine.py），
 * 差别只在求值那一步。拆成独立页的代价是：结果表、数据源策略/复权/排序/条数四个
 * 选项、溯源脚注各写一遍 —— 合并前已经出现列宽 84/86、「已降级」文案一处带原因
 * 一处不带这类漂移。
 *
 * ★ 经典策略为什么单开一页而不是塞进条件树：海龟/RPS 这类要做**多日形态识别 +
 * 横截面排名**，条件树只能做「单股单值比较」，表达不了（详见
 * backend/app/screener/classic.py 的模块说明）。
 *
 * 保留独立入口：`screen` / `formula` 两页仍在菜单里（多显示器 / 分栏对照时有用）。
 */

type Mode = "conditions" | "formula" | "classic";

export function ScreenWorkbench({ params }: PageProps) {
  const [mode, setMode] = useState<Mode>((params.mode as Mode) || "conditions");

  return (
    <div className={s.page} style={{ padding: 0, gap: 0 }}>
      <div style={{ padding: "var(--sp-2) var(--sp-2) 0" }}>
        <Tabs
          items={[
            { key: "conditions", label: "条件选股" },
            { key: "formula", label: "公式选股" },
            { key: "classic", label: "经典策略" },
          ]}
          value={mode}
          onChange={(k) => setMode(k as Mode)}
        />
      </div>
      {/* 各面板自带内边距与滚动（.panelHost），故此处不再包一层 padding */}
      {mode === "classic" ? (
        <ClassicPanel />
      ) : mode === "formula" ? (
        <FormulaPanel />
      ) : (
        <ConditionsPanel />
      )}
    </div>
  );
}

export default ScreenWorkbench;
