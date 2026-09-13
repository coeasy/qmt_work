// 通达信式外壳装配（v2 纯顶部菜单 3 行布局）：
//   row 1: TopBar (44px) — 菜单 + 搜索 + 跑马灯 + 券商状态 + WS延迟 + 时钟
//   row 2-3: Workbench (flex:1) — 实例级 TabBar + keep-alive 多 Tab 内容区
//
// v2 精简：删除 BrokerBar / BottomDock / QuickBar / SideTree。
// 底部综合栏 5 Tab（自选/板块/资金/成交/日志）全部提升为独立页面。
// 释放约 200px 垂直空间给工作区。
import { useEffect } from "react";
import { BrokerProvider } from "./BrokerContext.jsx";
import { PlatformProvider } from "./PlatformContext.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import TopBar from "./components/TopBar.jsx";
import Workbench from "./components/Workbench.jsx";
import CommandPalette from "./components/CommandPalette.jsx";
import HelpOverlay from "./components/HelpOverlay.jsx";
import { WorkspaceProvider } from "./store/workspace.jsx";
import { QuoteHubProvider } from "./lib/quoteHub.jsx";
import { ChartConfigProvider } from "./lib/chartConfig.jsx";
import { useHotkeys } from "./hooks/useHotkeys.js";

export default function App() {
  useHotkeys();

  // 兼容旧持久化键：清理历史遗留的「功能树显隐」标记
  useEffect(() => { localStorage.removeItem("qmt_fn_tree_hidden"); }, []);

  return (
    <BrokerProvider>
      <PlatformProvider>
        <QuoteHubProvider>
          <ChartConfigProvider>
            <WorkspaceProvider>
              <ErrorBoundary>
                <div className="tdx-app">
                  <TopBar />
                  <div className="tdx-body">
                    <div className="tdx-main">
                      <Workbench />
                    </div>
                  </div>
                  <CommandPalette />
                  <HelpOverlay />
                </div>
              </ErrorBoundary>
            </WorkspaceProvider>
          </ChartConfigProvider>
        </QuoteHubProvider>
      </PlatformProvider>
    </BrokerProvider>
  );
}
