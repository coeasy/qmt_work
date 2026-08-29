import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

// 全局兜底：任何未被 React.lazy 捕获的动态分片加载失败，
// 自动整页刷新一次以拉取最新构建（避免陈旧 entry 引用已清理的 hash 分片）。
(function installChunkReloadGuard() {
  // 每次整页加载（含自我修复触发的刷新）重新武装守卫，
  // 使后续多次重部署都能自我修复，而非只生效一次。
  try {
    Object.keys(sessionStorage).forEach((k) => {
      if (k.startsWith("qmt_chunk_reload_")) sessionStorage.removeItem(k);
    });
  } catch (_) {}

  const isChunkErr = (msg) =>
    /Failed to fetch dynamically imported module/i.test(msg) ||
    /Importing a module script failed/i.test(msg) ||
    /error loading dynamically imported module/i.test(msg);
  const tryReload = () => {
    if (sessionStorage.getItem("qmt_chunk_reload_global")) return;
    sessionStorage.setItem("qmt_chunk_reload_global", "1");
    location.reload();
  };
  window.addEventListener("unhandledrejection", (ev) => {
    const msg = String((ev.reason && ev.reason.message) || ev.reason || "");
    if (isChunkErr(msg)) tryReload();
  });
  window.addEventListener("error", (ev) => {
    const msg = String(ev.message || "");
    if (isChunkErr(msg)) tryReload();
  });
})();

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
