// vitest.setup.js — localStorage 兜底。
//
// 背景：本机 Node 以实验性 `--localstorage-file` 启动时，会注入一个残缺的全局
// localStorage（有 getItem/setItem 但无 clear()）。Vitest 的 jsdom 环境发现全局
// 已存在 localStorage 就不再覆盖，导致测试调用 placeholder 的 `localStorage.clear()`
// 时报 "localStorage.clear is not a function"。
//
// 这里仅在「全局 localStorage 缺失或不完整（无 clear）」时，用内存实现兜底；
// jsdom 提供完整 Storage 时保持原样不动。
const memStorage = (() => {
  let store = new Map();
  return {
    get length() { return store.size; },
    clear() { store.clear(); },
    getItem(k) { return store.has(String(k)) ? store.get(String(k)) : null; },
    setItem(k, v) { store.set(String(k), String(v)); },
    removeItem(k) { store.delete(String(k)); },
    key(i) { return Array.from(store.keys())[i] ?? null; },
  };
})();

const existing = globalThis.localStorage;
if (!existing || typeof existing.clear !== "function") {
  Object.defineProperty(globalThis, "localStorage", {
    value: memStorage,
    configurable: true,
    writable: true,
  });
}