import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// 构建产物直接输出到 backend/static，由 FastAPI 同源托管（桌面壳/浏览器均可访问）。
// 开发态通过 proxy 将 /api 与 /ws 转发到本地 21117 后端（默认端口，可在后端配置文件中修改）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:21117", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:21117", ws: true },
      "/mcp": { target: "http://127.0.0.1:21117", changeOrigin: true },
    },
  },
  build: {
    outDir: resolve(__dirname, "../backend/static"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        // P1 前端性能：vendor 分包 + echarts 独立 chunk（首屏不再一次拉全量图表库）
        manualChunks: {
          vendor: ["react", "react-dom"],
          charts: ["echarts"],
        },
      },
    },
  },
});
