import { useEffect } from "react";
import { TitleBar } from "@/shell/TitleBar";
import { Toolbar } from "@/shell/Toolbar";
import { DataPanel } from "@/shell/DataPanel";
import { Workspace } from "@/shell/Workspace/Workspace";
import { StatusBar } from "@/shell/StatusBar";
import { CommandPalette } from "@/shell/CommandPalette";
import { Hotkeys } from "@/shell/Hotkeys";
import { useWorkspaceStore } from "@/stores/workspace";
import { useUiStore } from "@/stores/ui";
import { initQuotePipeline } from "@/stores/quotes";
import { ensureECharts } from "@/charts/echartsSetup";
import s from "@/shell/shell.module.css";

/**
 * 应用外壳装配。
 *
 * 布局（方案 §4.3）：
 *   TitleBar（自绘标题栏：品牌 + 6 大业务域菜单 + 全局搜索 + 窗口按钮）
 *   Toolbar（上下文工具条）
 *   ├─ DataPanel（左栏数据，非导航）
 *   └─ Workspace（多 Tab + 拖拽分栏）
 *   StatusBar（连接/行情/时段/时钟）
 */
export function App() {
  const hydrate = useWorkspaceStore((st) => st.hydrate);
  const initUi = useUiStore((st) => st.init);

  useEffect(() => {
    initUi();
    hydrate();
    ensureECharts();
    const dispose = initQuotePipeline();
    return dispose;
  }, [initUi, hydrate]);

  return (
    <div className={s.shell}>
      <TitleBar />
      <Toolbar />
      <div className={s.main}>
        <DataPanel />
        <Workspace />
      </div>
      <StatusBar />
      <CommandPalette />
      <Hotkeys />
    </div>
  );
}
