// 通达信式外壳装配：
//   row 1: 顶菜单 (32px) — 主菜单 + 指数跑马灯
//   row 2: 状态条 (28px) — 当前券商连接 + 切换 + 快捷入口（TDX 风格 status bar）
//   row 3: 主体 (flex:1)  — 左功能树 + 中央 MDI 工作区
//   row 4: 底部综合栏 — 自选/板块/预警/成交/日志
//
// v3 改造新增两个全局 Provider：
//   · WorkspaceProvider —— 工作区单一状态源（实例级 tab + keep-alive + workspace.v3 持久化）
//   · QuoteHubProvider   —— 全应用唯一行情 WS 连接（引用计数多路复用，替代每页一条连接）
import { useEffect, useState } from "react";
import { BrokerProvider } from "./BrokerContext.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import BrokerBar from "./components/BrokerBar.jsx";
import MenuBar from "./components/MenuBar.jsx";
import FunctionTree from "./components/FunctionTree.jsx";
import Workbench from "./components/Workbench.jsx";
import BottomDock from "./components/BottomDock.jsx";
import CommandPalette from "./components/CommandPalette.jsx";
import HelpOverlay from "./components/HelpOverlay.jsx";
import { WorkspaceProvider } from "./store/workspace.jsx";
import { QuoteHubProvider } from "./lib/quoteHub.jsx";
import { useHotkeys } from "./hooks/useHotkeys.js";

export default function App() {
  useHotkeys();

  // 左侧功能树默认隐藏（折叠），用户可随时唤出；状态持久化到 localStorage。
  const [treeOpen, setTreeOpen] = useState(() => {
    const v = localStorage.getItem("qmt_fn_tree_hidden");
    // 未设置（首次）或显式 "1"（隐藏）→ 默认不展示
    return !(v === null || v === "1");
  });
  useEffect(() => {
    localStorage.setItem("qmt_fn_tree_hidden", treeOpen ? "0" : "1");
  }, [treeOpen]);

  // Ctrl/Cmd+B 切换功能树显隐（与 useHotkeys 派发的 tree:toggle 联动）
  useEffect(() => {
    const onToggle = () => setTreeOpen((o) => !o);
    window.addEventListener("tree:toggle", onToggle);
    return () => window.removeEventListener("tree:toggle", onToggle);
  }, []);

  return (
    <BrokerProvider>
      <QuoteHubProvider>
        <WorkspaceProvider>
          <ErrorBoundary>
            <div className="tdx-app">
              <MenuBar />
              <BrokerBar />
              <div className="tdx-body">
                <FunctionTree open={treeOpen} onClose={() => setTreeOpen(false)} />
                {!treeOpen && (
                  <button
                    className="fn-tree-fab"
                    title="显示功能树（Ctrl/Cmd+B）"
                    onClick={() => setTreeOpen(true)}
                  >☰</button>
                )}
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
      </QuoteHubProvider>
    </BrokerProvider>
  );
}
