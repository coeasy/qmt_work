import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom 未实现 ResizeObserver / matchMedia，测试环境下补桩
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
}

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// Node >= 22 自带实验性 `globalThis.localStorage` / `sessionStorage`：未加
// `--localstorage-file` 启动时它只是一个**空壳**（`getItem` 为 undefined）。
// 而 vitest 的 jsdom 环境只往 globalThis 填充「尚不存在」的 window 属性 ——
// 遇到这个已存在的空壳就跳过（该环境下 `window` 与 `globalThis` 是同一对象，
// 因此**两面都拿不到真实现**）。
//
// 症状：只要用例里出现裸 `localStorage`，就报
// `TypeError: localStorage.getItem is not a function`，与用例逻辑无关。
// 实测（Node v25.2.1）：48 个测试文件 / 458 个用例**全红**；换 Node v22.22.2
// （工具链锁定版本）全绿 —— 失败根因在运行时而非产品代码。
//
// 处理：当前 storage 不可用时，装一个**内存实现**。这不改变产品行为
// （产品只依赖 Storage 接口语义），只保证测试在任意 Node 上得到同一前提。
function makeMemoryStorage(): Storage {
  const map = new Map<string, string>();
  const api = {
    get length(): number {
      return map.size;
    },
    clear(): void {
      map.clear();
    },
    getItem(key: string): string | null {
      const k = String(key);
      return map.has(k) ? (map.get(k) as string) : null;
    },
    key(index: number): string | null {
      return Array.from(map.keys())[index] ?? null;
    },
    removeItem(key: string): void {
      map.delete(String(key));
    },
    setItem(key: string, value: string): void {
      map.set(String(key), String(value));
    },
  };
  return api as Storage;
}

function _isUsableStorage(v: unknown): boolean {
  const s = v as Storage | undefined;
  return !!s && typeof s.getItem === "function" && typeof s.setItem === "function";
}

function ensureStorage(name: "localStorage" | "sessionStorage"): void {
  const g = globalThis as unknown as Record<string, unknown>;
  if (_isUsableStorage(g[name]) && _isUsableStorage((window as unknown as Record<string, unknown>)[name])) {
    return;
  }
  const shim = makeMemoryStorage();
  for (const target of [globalThis, typeof window === "undefined" ? null : window]) {
    if (!target || _isUsableStorage((target as unknown as Record<string, unknown>)[name])) continue;
    try {
      Object.defineProperty(target, name, {
        value: shim,
        configurable: true,
        writable: true,
      });
    } catch {
      try {
        (target as unknown as Record<string, unknown>)[name] = shim;
      } catch {
        // 宿主把属性定义为不可配置：保持现状，不让 setup 自身崩掉。
      }
    }
  }
}

ensureStorage("localStorage");
ensureStorage("sessionStorage");
