import { useState } from "react";
import { Tabs } from "@/design/primitives";
import type { PageProps } from "@/app/routes";
import { AutoPicks } from "./screen/AutoPicks";
import { ClassicPanel, ConditionsPanel, FormulaPanel } from "./screen/ScreenPanels";
import s from "../domain.module.css";

/**
 * 选股工作台 —— 把「条件选股 / 公式选股 / 经典策略」合并到一个界面。
 *
 * 为什么合并：这三页问的是同一个问题（「按我的规则筛出哪些票」），区别只是规则的
 * 写法 —— 条件树 JSON、类终端公式、还是现成的经典形态策略。后端也共用同一套
 * 「解析股票池 → 批量取 K 线 → 求值 → 溯源」编排（app/screener/engine.py），
 * 差别只在求值那一步。拆成独立页的代价是：结果表、数据源策略/复权/排序/条数四个
 * 选项、溯源脚注各写一遍 —— 合并前已经出现列宽 84/86、「已降级」文案一处带原因
 * 一处不带这类漂移。
 *
 * ★ 经典策略为什么单开一页而不是塞进条件树：海龟/RPS 这类要做**多日形态识别 +
 * 横截面排名**，条件树只能做「单股单值比较」，表达不了（详见
 * backend/app/screener/classic.py 的模块说明）。
 *
 * ★ 新增第四个页签「自动选股」：它展示的是定时任务跑出来的**历史结果**，
 *   与上面三种「现在就跑一次」的形态互补（一个看结果、三个定规则）。之前它被
 *   单独挂在菜单里，与「选股工作台」并列 —— 用户看到「选股工作台 / 自动选股 /
 *   条件选股 / 公式选股」四个入口，实际是同一件事的四种说法。现在合成一个入口，
 *   `screen` / `formula` / `auto_picks` 三个独立页改为 `menu:false`（仍注册，
 *   供多显示器 / 分栏对照时从命令面板直达）。
 */

type Mode = "conditions" | "formula" | "classic" | "auto";

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
            { key: "auto", label: "自动选股结果" },
          ]}
          value={mode}
          onChange={(k) => setMode(k as Mode)}
        />
      </div>
      {/* 各面板自带内边距与滚动（.panelHost），故此处不再包一层 padding */}
      {mode === "auto" ? (
        <AutoPicks bare />
      ) : mode === "classic" ? (
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
