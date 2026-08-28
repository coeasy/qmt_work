// ESLint 扁平配置（flat config，ESLint 9+）。前端门禁定位（C3/P2-3）：
// 仅查真实 bug —— 未定义变量（no-undef，可抓漏导入）、命名遮蔽（no-shadow，
// 抓 setInterval 遮蔽陷阱）、React hooks 误用（rules-of-hooks）。
// 与后端 ruff 策略一致：只拦真实问题，不搞风格洁癖，避免噪音淹没信号。
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    ignores: [
      "node_modules",
      "dist",
      "dist-electron",
      "backend",
      "**/*.min.js",
      // electron 侧为 CommonJS + Node 环境，独立于前端门禁
      "electron/**",
    ],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.{js,jsx}", "vite.config.js", "vitest.config.js", "eslint.config.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
        // vite/eslint 配置文件运行于 Node
        __dirname: "readonly",
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    linterOptions: {
      // 代码库存在大量旧式 /* eslint-disable */ 注释，不计为问题
      reportUnusedDisableDirectives: "off",
    },
    rules: {
      // ---- 真实 bug 门禁 ----
      "no-undef": "error",
      "no-shadow": "error",
      "react-hooks/rules-of-hooks": "error",
      // ---- 噪音规则关闭（不属本次门禁目标）----
      "no-unused-vars": "off",
      "no-empty": "off",
      "no-useless-escape": "off",
      "no-prototype-builtins": "off",
      "no-case-declarations": "off",
      "no-cond-assign": "off",
      "no-constant-condition": "off",
      "no-async-promise-executor": "off",
      "no-unreachable": "off",
      "no-fallthrough": "off",
      "no-irregular-whitespace": "off",
      "no-control-regex": "off",
      "no-misleading-character-class": "off",
      "no-extra-boolean-cast": "off",
      "no-extra-semi": "off",
      "no-useless-catch": "off",
      "no-self-assign": "off",
      "no-await-in-loop": "off",
      "no-ex-assign": "off",
      "no-import-assign": "off",
      "no-dupe-args": "off",
      "no-dupe-keys": "off",
      "no-func-assign": "off",
      "valid-typeof": "off",
      "getter-return": "off",
      "no-redeclare": "off",
      "no-duplicate-case": "off",
      "no-unexpected-multiline": "off",
      "no-unsafe-finally": "off",
      "no-unsafe-negation": "off",
      "no-unsafe-optional-chaining": "off",
      "no-obj-calls": "off",
      "no-sparse-arrays": "off",
    },
  },
];