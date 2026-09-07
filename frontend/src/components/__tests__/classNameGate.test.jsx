// className 差集 CI 门禁：JSX 中静态使用的类名必须在 styles.css 有定义。
// 白名单 = 已知的语义标记/动态模板串产物（无害）。防止"改了 JSX 忘了写样式"再漂移。
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const SRC = join(__dirname, "..", "..");
const WHITELIST = new Set([
  "boards-page", "etfs-page", "etf-d-chart", "cmd-label", "form-tip",
  "io-trend", "mf-replay", "dock-watch", "wb-cmd-btn", "wb-menu-layout",
  "otm-card", "otm-pre", "market-page", "page", "hub-body",
  "btn", "link", "k", "v", "modal-mask", "modal",  // 模板串/对象字面量误捕或通用标记
]);

function walk(dir, out = []) {
  for (const f of readdirSync(dir)) {
    const fp = join(dir, f);
    if (statSync(fp).isDirectory()) walk(fp, out);
    else if (/\.(jsx|js)$/.test(f) && !/\.test\./.test(f)) out.push(fp);
  }
  return out;
}

describe("className 差集门禁", () => {
  it("JSX 静态类名均能在 styles.css 或白名单中找到", () => {
    const css = readFileSync(join(SRC, "styles.css"), "utf-8");
    const defined = new Set([...css.matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1]));
    const missing = new Set();
    for (const file of walk(SRC)) {
      const src = readFileSync(file, "utf-8");
      for (const m of src.matchAll(/className=["'`]([^"'`]+)["'`]/g)) {
        for (const cls of m[1].split(/\s+/)) {
          if (!cls || cls.includes("$") || cls.includes("{")) continue; // 动态模板串跳过
          if (!defined.has(cls) && !WHITELIST.has(cls)) missing.add(cls);
        }
      }
    }
    expect([...missing], `未定义的类名: ${[...missing].join(", ")}`).toEqual([]);
  });
});
