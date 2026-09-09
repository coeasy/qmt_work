// 通达信式外壳装配：
//   row 1: 顶菜单 (32px) — 主菜单 + 指数跑马灯
//   row 2: 状态条 (28px) — 当前券商连接 + 切换 + 快捷入口（TDX 风格 status bar）
//   row 3: 主体 (flex:1)  — 中央 MDI 工作区
//   row 4: 底部综合栏 — 自选/板块/预警/成交/日志
//
// v3 改造新增两个全局 Provider：
//   · WorkspaceProvider —— 工作区单一状态源（实例级 tab + keep-alive + workspace.v3 持久化）
//   · QuoteHubProvider   —— 全应用唯一行情 WS 连接（引用计数多路复用，替代每页一条连接）
//
// v4：移除左侧功能树（与顶部菜单栏完全重复，且悬浮唤出按钮会遮挡标签栏）。
//     导航统一收敛到三条通道：顶菜单下拉 / GlobalSearch / 命令面板（Ctrl+K 或 Ctrl+B）。
import { useEffect } from "react";
import { BrokerProvider } from "./BrokerContext.jsx";
import { PlatformProvider } from "./PlatformContext.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import BrokerBar from "./components/BrokerBar.jsx";
import MenuBar from "./components/MenuBar.jsx";
import Workbench from "./components/Workbench.jsx";
import BottomDock from "./components/BottomDock.jsx";
import CommandPalette from "./components/CommandPalette.jsx";
import HelpOverlay from "./components/HelpOverlay.jsx";
import { WorkspaceProvider } from "./store/workspace.jsx";
import { QuoteHubProvider } from "./lib/quoteHub.jsx";
import { ChartConfigProvider } from "./lib/chartConfig.jsx";
import { useHotkeys } from "./hooks/useHotkeys.js";

export default function App() {
  useHotkeys();

  // 兼容旧持久化键：清理历史遗留的「功能树显隐」标记，避免脏数据
  useEffect(() => { localStorage.removeItem("qmt_fn_tree_hidden"); }, []);

  return (
    <BrokerProvider>
      <PlatformProvider>
        <QuoteHubProvider>
          <ChartConfigProvider>
            <WorkspaceProvider>
              <ErrorBoundary>
                <div className="tdx-app">
                  <MenuBar />
                  <BrokerBar />
                  <div className="tdx-body">
                    <div className="tdx-main">
                      <Workbench />
                    </div>
                  </div>
                  <BottomDock />
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
