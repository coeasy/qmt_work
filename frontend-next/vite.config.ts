import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// qmt_work 专业交易终端 · 全新前端工程（已退役旧 frontend/，现为唯一主前端）
//
// 与旧 frontend/ 的关键差异（现已落地为主前端）：
// - 产物直接输出到 backend/static/，由后端 app/main.py 的 catch-all "/" 挂载服务，
//   故 **build 时 base 必须是 "/"**（相对/绝对根路径），否则 /assets/* 无法解析。
// - 开发端口 5273（独立 dev server，避免与残留旧工程冲突）。
// - 依赖分包：react 核心 / 图表（echarts + klinecharts）独立 chunk
export default defineConfig({
  // 生产构建与开发期均走根路径：vite base="/" 时 HTML 内 /assets/* 绝对路径可被
  // 后端 StaticFiles("/") 正确解析。
  // （旧实现按 command 区分 "/next/" 灰度 base；退役旧 frontend 后固定为根目录。）
  base: "/",
  plugins: [react()],
  resolve: {
    alias: { "@": resolve(__dirname, "src") },
  },
  server: {
    port: 5273,
    proxy: {
      "/api": { target: "http://127.0.0.1:21118", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:21118", ws: true },
      "/mcp": { target: "http://127.0.0.1:21118", changeOrigin: true },
    },
  },
  build: {
    outDir: resolve(__dirname, "../backend/static"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "zustand"],
          // 图表库拆成独立 chunk：只随图表类页面按需加载，
          // 不进入主包（Toolbar 等外壳组件已改为从 shared/periods 取常量）
          echarts: ["echarts", "echarts/core", "echarts/charts", "echarts/components"],
          klinecharts: ["klinecharts"],
        },
      },
    },
  },
});
