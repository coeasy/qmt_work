// 通达信式外壳装配：
//   row 1: 顶菜单 (32px) — 主菜单 + 指数跑马灯
//   row 2: 状态条 (28px) — 当前券商连接 + 切换 + 快捷入口（TDX 风格 status bar）
//   row 3: 主体 (flex:1)  — 左功能树 + 中央 MDI 工作区
//   row 4: 底部综合栏 — 自选/板块/预警/成交/日志
import { BrokerProvider } from "./BrokerContext.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import BrokerBar from "./components/BrokerBar.jsx";
import MenuBar from "./components/MenuBar.jsx";
import FunctionTree from "./components/FunctionTree.jsx";
import Workbench from "./components/Workbench.jsx";
import BottomDock from "./components/BottomDock.jsx";
import CommandPalette from "./components/CommandPalette.jsx";
import HelpOverlay from "./components/HelpOverlay.jsx";
import { useHotkeys } from "./hooks/useHotkeys.js";

export default function App() {
  useHotkeys();
  return (
    <BrokerProvider>
      <ErrorBoundary>
        <div className="tdx-app">
          <MenuBar />
          <BrokerBar />
          <div className="tdx-body">
            <FunctionTree />
            <div className="tdx-main">
              <Workbench />
            </div>
          </div>
          <BottomDock />
          <CommandPalette />
          <HelpOverlay />
        </div>
      </ErrorBoundary>
    </BrokerProvider>
  );
}
