import { defineConfig } from "vitest/config";

// 纯函数单元测试（C3/P2-3）：jsdom 不需要，node 环境即可跑 format 等纯函数。
// 运行：npm test（vitest run）。
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/lib/__tests__/**/*.test.js"],
  },
});