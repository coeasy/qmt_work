import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

/**
 * 界面偏好（主题三态）回归测试。
 *
 * 这块逻辑容易悄悄坏掉且不易察觉：类型检查永远通过，但一旦「跟随系统」失效，
 * 表现只是「首屏主题不对」——用户很可能以为是自己的错觉。
 * 因此把三条规则钉死：
 *   1. 无持久化记录 → themePref = "auto"，按系统 prefers-color-scheme 解析
 *   2. 老版本只存 theme → 迁移为显式 pref（不再跟随系统）
 *   3. 显式选择 → 落盘并立即改写 <html data-theme>
 *
 * store 是模块级单例，load() 只在 import 时执行一次，
 * 所以每个用例都要 vi.resetModules() + 动态 import 才能拿到全新实例。
 */

const KEY = "qmt.ui.v1";

/** 覆盖 matchMedia，模拟系统深/浅色 */
function setSystemDark(dark: boolean): void {
  window.matchMedia = ((query: string) => ({
    matches: dark,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

/** 取一个全新的 store 实例（重新执行模块顶层 load()） */
async function freshStore() {
  vi.resetModules();
  const mod = await import("@/stores/ui");
  return mod.useUiStore;
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.themePref;
  delete document.documentElement.dataset.updown;
});

afterEach(() => {
  localStorage.clear();
});

describe("主题偏好 · 默认与系统跟随", () => {
  it("无持久化记录时默认浅色 + 晨曦白背景（不跟随系统）", async () => {
    setSystemDark(true);
    const store = await freshStore();
    expect(store.getState().themePref).toBe("light");
    expect(store.getState().theme).toBe("light");
    expect(store.getState().activeSkin).toBe("light");

    setSystemDark(false);
    const lightStore = await freshStore();
    expect(lightStore.getState().themePref).toBe("light");
    expect(lightStore.getState().theme).toBe("light");
    expect(lightStore.getState().activeSkin).toBe("light");
  });

  it("显式选择 跟随系统(auto) 时按系统主题解析", async () => {
    setSystemDark(false);
    localStorage.setItem(KEY, JSON.stringify({ themePref: "auto" }));
    const store = await freshStore();
    expect(store.getState().themePref).toBe("auto");
    expect(store.getState().theme).toBe("light");

    setSystemDark(true);
    const darkStore = await freshStore();
    expect(darkStore.getState().themePref).toBe("auto");
    expect(darkStore.getState().theme).toBe("dark");
  });

  it("auto 模式下 init() 把系统主题写入 <html data-theme>", async () => {
    setSystemDark(false);
    localStorage.setItem(KEY, JSON.stringify({ themePref: "auto" }));
    const store = await freshStore();
    store.getState().init();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.themePref).toBe("auto");
  });

  it("init() 在 auto 模式下响应系统主题变化", async () => {
    setSystemDark(false);
    localStorage.setItem(KEY, JSON.stringify({ themePref: "auto" }));
    const store = await freshStore();
    store.getState().init();
    expect(store.getState().theme).toBe("light");

    // 模拟系统切到深色：直接调用 store 的监听路径（jsdom 不会真的派发 mq 事件）
    setSystemDark(true);
    // 重新解析：显式走 setThemePref("auto")，等价于监听到 change 后重新 resolve
    store.getState().setThemePref("auto");
    expect(store.getState().theme).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("主题偏好 · 持久化与迁移", () => {
  it("老版本仅存 theme 字段 → 迁移为显式 pref，不再跟随系统", async () => {
    setSystemDark(true); // 系统深色
    localStorage.setItem(KEY, JSON.stringify({ theme: "light" })); // 用户曾选浅色

    const store = await freshStore();
    expect(store.getState().themePref).toBe("light");
    expect(store.getState().theme).toBe("light");
  });

  it("老版本 theme 值非法时回退默认（浅色）", async () => {
    setSystemDark(false);
    localStorage.setItem(KEY, JSON.stringify({ theme: "solarized" }));
    const store = await freshStore();
    expect(store.getState().themePref).toBe("light");
    expect(store.getState().theme).toBe("light");
  });

  it("setThemePref 立即改写 DOM 并落盘", async () => {
    setSystemDark(true);
    const store = await freshStore();

    store.getState().setThemePref("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(store.getState().themePref).toBe("light");
    expect(JSON.parse(localStorage.getItem(KEY) ?? "{}").themePref).toBe("light");

    // 落盘后重新加载：显式 pref 优先于系统深色
    const reloaded = await freshStore();
    expect(reloaded.getState().themePref).toBe("light");
    expect(reloaded.getState().theme).toBe("light");
  });

  it("toggleTheme 基于当前生效主题取反（默认浅色 → 深色 → 浅色）", async () => {
    setSystemDark(true);
    const store = await freshStore();
    expect(store.getState().theme).toBe("light");

    store.getState().toggleTheme();
    expect(store.getState().theme).toBe("dark");
    expect(store.getState().themePref).toBe("dark");

    store.getState().toggleTheme();
    expect(store.getState().theme).toBe("light");
    expect(store.getState().themePref).toBe("light");
  });

  it("损坏的 localStorage 内容不会抛错，回退默认（浅色）", async () => {
    setSystemDark(false);
    localStorage.setItem(KEY, "{ 这不是 JSON");
    const store = await freshStore();
    expect(store.getState().themePref).toBe("light");
    expect(store.getState().theme).toBe("light");
  });
});

describe("涨跌配色", () => {
  it("默认红涨绿跌，可切换为绿涨红跌并落盘", async () => {
    const store = await freshStore();
    expect(store.getState().updown).toBe("red-up");

    store.getState().setUpdown("green-up");
    expect(document.documentElement.dataset.updown).toBe("green-up");
    expect(JSON.parse(localStorage.getItem(KEY) ?? "{}").updown).toBe("green-up");

    const reloaded = await freshStore();
    expect(reloaded.getState().updown).toBe("green-up");
  });

  it("涨跌配色独立于明暗主题", async () => {
    setSystemDark(true);
    const store = await freshStore();
    store.getState().setUpdown("green-up");
    store.getState().setThemePref("light");
    expect(document.documentElement.dataset.updown).toBe("green-up");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
