import { create } from "zustand";

/**
 * 界面偏好 store（主题 / 涨跌色 / 左栏数据面板）。
 * 主题与涨跌色通过 <html> 的 data-* 属性驱动，设计令牌自动生效。
 *
 * ★ 主题三态：`auto`（跟随系统）/ `dark` / `light`。
 *   - 首次进入（无持久化记录）默认 `auto`，跟随操作系统 `prefers-color-scheme`；
 *     若系统为浅色而终端强制深色，会出现「IDE 浅色 / 终端深色」的割裂感。
 *   - 用户在状态栏或设置页显式选择后，pref 落盘，此后不再跟随系统。
 *   - `auto` 模式下监听系统主题变化并实时切换（如夜间自动转深色）。
 *   - 持久化键为 `qmt.ui.v1`；老版本只存了 `theme`，load() 会迁移为显式 pref。
 *
 * ★ 无闪烁（no-FOUC）：`index.html` 内联脚本在 React 挂载前就按同一套规则
 *   写好 `data-theme`，避免「先深色闪一下再变浅色」。
 *   两处规则必须保持一致，改这里记得同步 index.html。
 */

export type Theme = "dark" | "light";
export type ThemePref = "auto" | Theme;
export type UpDownMode = "red-up" | "green-up";
export type DataPanelTab = "watchlist" | "orderbook" | "alerts";

const STORAGE_KEY = "qmt.ui.v1";
const DARK_QUERY = "(prefers-color-scheme: dark)";

interface Persisted {
  themePref: ThemePref;
  updown: UpDownMode;
  dataPanelTab: DataPanelTab;
  dataPanelOpen: boolean;
}

/** 读取系统主题；matchMedia 在极老环境或 SSR 下可能缺失，兜底 dark */
function systemTheme(): Theme {
  try {
    return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
  } catch {
    return "dark";
  }
}

function resolve(pref: ThemePref): Theme {
  return pref === "auto" ? systemTheme() : pref;
}

function isTheme(t: unknown): t is Theme {
  return t === "dark" || t === "light";
}

function load(): Persisted {
  const fallback: Persisted = {
    themePref: "auto",
    updown: "red-up",
    dataPanelTab: "watchlist",
    dataPanelOpen: true,
  };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return fallback;
    const p = JSON.parse(raw) as Partial<Persisted> & { theme?: unknown };
    // 迁移：旧版本写的是已解析的 theme，没有 themePref
    let themePref: ThemePref = "auto";
    if (p.themePref === "auto" || isTheme(p.themePref)) themePref = p.themePref;
    else if (isTheme(p.theme)) themePref = p.theme;
    return {
      themePref,
      updown: p.updown === "green-up" ? "green-up" : "red-up",
      dataPanelTab:
        p.dataPanelTab === "orderbook" || p.dataPanelTab === "alerts"
          ? p.dataPanelTab
          : "watchlist",
      dataPanelOpen: p.dataPanelOpen !== false,
    };
  } catch {
    return fallback;
  }
}

function apply(p: Persisted): Theme {
  const root = document.documentElement;
  const theme = resolve(p.themePref);
  root.dataset.theme = theme;
  root.dataset.themePref = p.themePref;
  root.dataset.updown = p.updown;
  return theme;
}

interface UiState extends Persisted {
  /** 已解析的实际主题（供需要判断明暗的组件读取） */
  theme: Theme;
  commandOpen: boolean;

  setThemePref: (p: ThemePref) => void;
  /** 直接指定明暗（会覆盖 auto） */
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  setUpdown: (m: UpDownMode) => void;
  setDataPanelTab: (t: DataPanelTab) => void;
  toggleDataPanel: () => void;
  setCommandOpen: (open: boolean) => void;
  /** 启动时调用：把持久化偏好写入 DOM，并挂上系统主题监听 */
  init: () => void;
}

const initial = load();

function persist(s: Persisted): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* 忽略 */
  }
}

/** 系统主题监听句柄（仅 auto 模式生效） */
let mq: MediaQueryList | null = null;
let mqHandler: (() => void) | null = null;

export const useUiStore = create<UiState>((set, get) => ({
  ...initial,
  theme: resolve(initial.themePref),
  commandOpen: false,

  setThemePref(p) {
    const next = { ...pick(get()), themePref: p };
    const theme = apply(next);
    persist(next);
    set({ themePref: p, theme });
  },

  setTheme(t) {
    get().setThemePref(t);
  },

  toggleTheme() {
    // 从当前「实际显示」的明暗取反，语义最直观
    get().setTheme(get().theme === "dark" ? "light" : "dark");
  },

  setUpdown(m) {
    const next = { ...pick(get()), updown: m };
    apply(next);
    persist(next);
    set({ updown: m });
  },

  setDataPanelTab(t) {
    const next = { ...pick(get()), dataPanelTab: t };
    persist(next);
    set({ dataPanelTab: t });
  },

  toggleDataPanel() {
    const open = !get().dataPanelOpen;
    const next = { ...pick(get()), dataPanelOpen: open };
    persist(next);
    set({ dataPanelOpen: open });
  },

  setCommandOpen(open) {
    set({ commandOpen: open });
  },

  init() {
    const theme = apply(pick(get()));
    set({ theme });

    // 挂系统主题监听：只有 auto 模式才跟随，显式选择后不打扰用户
    if (!mq) {
      try {
        mq = window.matchMedia(DARK_QUERY);
        mqHandler = () => {
          if (get().themePref !== "auto") return;
          const next = apply(pick(get()));
          set({ theme: next });
        };
        mq.addEventListener("change", mqHandler);
      } catch {
        /* 环境不支持则忽略，退化为静态主题 */
      }
    }
  },
}));

function pick(s: UiState): Persisted {
  return {
    themePref: s.themePref,
    updown: s.updown,
    dataPanelTab: s.dataPanelTab,
    dataPanelOpen: s.dataPanelOpen,
  };
}
