import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "@/app/App";
import { ErrorBoundary } from "@/app/ErrorBoundary";
import "@/design/tokens.css";
import "@/design/skins.css";
import "@/design/base.css";

const el = document.getElementById("root");
if (!el) throw new Error("缺少 #root 挂载点");

createRoot(el).render(
  <StrictMode>
    {/* 壳级兜底：即使 App 自身崩了，也给用户一条可操作的提示，而不是白窗 */}
    <ErrorBoundary scope="app">
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
