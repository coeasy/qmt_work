import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 单元测试运行：npm test（vitest run）。
// 默认 node 环境（format 等纯函数不需要 DOM）；需要 DOM 的用例在文件头用
// `// @vitest-environment jsdom` 单独声明（如 useActiveInterval 的定时器语义测试）。
export default defineConfig({
  // react 插件：让 hook 测试里的 JSX 能被正确转译（同 vite.config.js）
  plugins: [react()],
  test: {
    environment: "node",
    setupFiles: ["./vitest.setup.js"],
    include: [
      "src/lib/__tests__/**/*.test.js",
      // hook 测试含 JSX，必须用 .jsx 后缀（plugin-react 只对 jsx/tsx 生效）
      "src/hooks/__tests__/**/*.test.jsx",
    ],
  },
});