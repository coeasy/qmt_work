import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * 模块路径必须能真正解析到文件（含 `@/` 别名与 `*.module.css`）。
 *
 * 存在意义（本轮实测踩到）：`tsc` 不会校验 `*.module.css` 这类**通配声明**
 * （`vite/client` 的 `declare module "*.module.css"` 让任意路径都"合法"），
 * 而 vitest 用例从不真正 import 页面模块（`routes.tsx` 用 `lazy()`，测试只读源码文本）
 * ⇒ 一个写错的相对路径（`./domain.module.css` 应为 `../domain.module.css`）可以
 * **同时骗过类型检查与全部单测**，直到 `vite build` 才炸（实测报
 * `Could not resolve "./domain.module.css" from "src/domains/market/OrderBook.tsx"`）。
 *
 * 本用例把这一步提前到门禁里：比构建快 100 倍，且能精确指出是哪个文件哪一行。
 */

const SRC = resolve(__dirname, "../src");

function collectSources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) collectSources(p, out);
    else if (/\.tsx?$/.test(p)) out.push(p);
  }
  return out;
}

/** 相对/别名说明符 → 候选绝对路径（按 TS/打包器实际的解析顺序） */
function candidates(spec: string, fromFile: string): string[] {
  const base = spec.startsWith("@/")
    ? join(SRC, spec.slice(2))
    : resolve(dirname(fromFile), spec);
  return [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    `${base}.js`,
    `${base}.jsx`,
    join(base, "index.ts"),
    join(base, "index.tsx"),
  ];
}

/** 抓 `import ... from "x"` / `import "x"` / `import("x")` / `export ... from "x"` */
function specifiersOf(code: string): string[] {
  const out: string[] = [];
  const patterns = [
    /(?:^|[^\w$])import\s+(?:type\s+)?[^"'`;]*?from\s*["']([^"']+)["']/gm,
    /(?:^|[^\w$])import\s*["']([^"']+)["']/gm,
    /(?:^|[^\w$])import\s*\(\s*["']([^"']+)["']\s*\)/gm,
    /(?:^|[^\w$])export\s+(?:type\s+)?[^"'`;]*?from\s*["']([^"']+)["']/gm,
  ];
  for (const re of patterns) {
    for (const m of code.matchAll(re)) {
      const s = m[1];
      if (s && (s.startsWith(".") || s.startsWith("@/"))) out.push(s);
    }
  }
  return out;
}

describe("模块路径可解析", () => {
  const files = collectSources(SRC);

  it("src 下有可扫描的源文件（守卫：避免路径写错导致空跑假绿）", () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it("所有相对 / `@/` 导入都能解析到真实文件", () => {
    const broken: string[] = [];
    for (const f of files) {
      const code = readFileSync(f, "utf-8");
      for (const spec of specifiersOf(code)) {
        if (!candidates(spec, f).some((c) => existsSync(c))) {
          broken.push(`${f.slice(SRC.length + 1)} → ${spec}`);
        }
      }
    }
    expect(broken, `以下导入无法解析：\n${broken.join("\n")}`).toEqual([]);
  });
});
