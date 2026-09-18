import { create } from "zustand";
import {
  CUSTOM_SKIN_ID,
  DEFAULT_SKIN_ID,
  SKIN_TOKEN_KEYS,
  deriveSkinTokens,
  isDarkColor,
  presetById,
} from "@/design/skins";

/**
 * 界面偏好 store（主题 / 背景配色 / 涨跌色 / 左栏数据面板）。
 * 主题与涨跌色通过 <html> 的 data-* 属性驱动，设计令牌自动生效。
 *
 * ★ 主题三态：`auto`（跟随系统）/ `dark` / `light`。
 *   - 首次进入（无持久化记录）默认 `dark` + `通达信黑` 背景（A 股终端标配），
 *     直接进入专业终端的深色工作态，无需先手动切主题。
 *   - 用户在状态栏或设置页显式选择后可以切到 `跟随系统` / `浅色` / 其它皮肤，
 *     pref 落盘，此后按用户选择生效。
 *   - `auto` 模式下监听系统主题变化并实时切换（如夜间自动转深色）。
 *   - 持久化键为 `qmt.ui.v1`；老版本只存了 `theme`，load() 会迁移为显式 pref。
 *
 * ★ 背景配色（皮肤）：`skin` 是预设皮肤 id 或 `custom`（自定义背景色）。
 *   预设皮肤靠 `<html data-skin>` 生效（design/skins.css），零 JS 计算；
 *   自定义皮肤由 `deriveSkinTokens` 派生**成套**令牌写成内联变量。
 *   **只在明暗方向与当前主题一致时才生效** —— 否则会出现「深色皮肤 + 浅色主题」
 *   这类错配（背景黑、图表白）。错配时自动退回主题默认令牌。
 *
 * ★ 无闪烁（no-FOUC）：`public/theme-boot.js` 在 React 挂载前按同一套规则写好
 *   `data-theme` / `data-skin` 与自定义令牌，避免「先深色闪一下再变浅色」。
 *   两处规则必须保持一致，改这里记得同步那个文件。
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
  /** 预设皮肤 id / "custom"（自定义背景色）/ ""（用主题默认令牌） */
  skin: string;
  /** 自定义背景色（仅 skin==="custom" 时有意义） */
  customBg: string;
  /**
   * 自定义皮肤**派生好的**令牌快照。
   * 落盘它而不是只存颜色，是为了让 theme-boot.js 能直接搬运 ——
   * 预热脚本里不重复实现派生逻辑，单一真源仍在 design/skins.ts。
   */
  customTokens: Record<string, string>;
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
    themePref: "dark",
    updown: "red-up",
    dataPanelTab: "watchlist",
    dataPanelOpen: true,
    skin: DEFAULT_SKIN_ID,
    customBg: "#0a0a0a",
    customTokens: {},
  };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return fallback;
    const p = JSON.parse(raw) as Partial<Persisted> & { theme?: unknown };
    // 迁移：旧版本写的是已解析的 theme，没有 themePref；
    // 非法 / 缺失的 theme 回退到新默认（深色 + 通达信黑），而非 auto
    let themePref: ThemePref = "dark";
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
      skin: typeof p.skin === "string" ? p.skin : "",
      customBg: typeof p.customBg === "string" && p.customBg ? p.customBg : "#0a0a0a",
      customTokens: {},
    };
  } catch {
    return fallback;
  }
}

/**
 * 当前偏好下，皮肤是否真的生效，以及该写哪些内联令牌。
 *
 * 抽成纯函数是为了让「错配必须退回默认」这条规则可被单测直接锁定，
 * 而不是散在 apply() 里靠人读。
 */
export function resolveSkin(
  skin: string,
  customBg: string,
  theme: Theme,
): { active: string; tokens: Record<string, string> | null } {
  if (skin === CUSTOM_SKIN_ID) {
    const tokens = deriveSkinTokens(customBg);
    if (!tokens) return { active: "", tokens: null };
    const tone: Theme = isDarkColor(customBg) ? "dark" : "light";
    return tone === theme ? { active: CUSTOM_SKIN_ID, tokens } : { active: "", tokens: null };
  }
  const preset = presetById(skin);
  return preset && preset.tone === theme ? { active: skin, tokens: null } : { active: "", tokens: null };
}

function apply(p: Persisted): Theme {
  const root = document.documentElement;
  const theme = resolve(p.themePref);
  root.dataset.theme = theme;
  root.dataset.themePref = p.themePref;
  root.dataset.updown = p.updown;

  const { active, tokens } = resolveSkin(p.skin, p.customBg, theme);
  // 先清掉上一次的内联令牌，否则从自定义切回预设时会残留旧配色
  for (const k of SKIN_TOKEN_KEYS) root.style.removeProperty(k);
  if (tokens) {
    for (const [k, v] of Object.entries(tokens)) root.style.setProperty(k, v);
  }
  if (active) root.dataset.skin = active;
  else delete root.dataset.skin;
  return theme;
}

interface UiState extends Persisted {
  /** 已解析的实际主题（供需要判断明暗的组件读取） */
  theme: Theme;
  /** 当前真正生效的皮肤 id（错配时为 ""） */
  activeSkin: string;
  commandOpen: boolean;

  setThemePref: (p: ThemePref) => void;
  /** 直接指定明暗（会覆盖 auto） */
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  setUpdown: (m: UpDownMode) => void;
  /** 选预设皮肤；同时把明暗切到该皮肤的方向，避免错配 */
  setSkin: (id: string) => void;
  /** 自定义背景色：切到 custom 皮肤并按背景明暗对齐主题方向 */
  setCustomBg: (hex: string) => void;
  setDataPanelTab: (t: DataPanelTab) => void;
  toggleDataPanel: () => void;
  setCommandOpen: (open: boolean) => void;
  /** 启动时调用：把持久化偏好写入 DOM，并挂上系统主题监听 */
  init: () => void;
}

const initial = load();

function pick(s: UiState): Persisted {
  const tokens = s.skin === CUSTOM_SKIN_ID ? deriveSkinTokens(s.customBg) : null;
  return {
    themePref: s.themePref,
    updown: s.updown,
    dataPanelTab: s.dataPanelTab,
    dataPanelOpen: s.dataPanelOpen,
    skin: s.skin,
    customBg: s.customBg,
    customTokens: tokens ?? {},
  };
}

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
  activeSkin: resolveSkin(initial.skin, initial.customBg, resolve(initial.themePref)).active,
  commandOpen: false,

  setThemePref(p) {
    const next = { ...pick(get()), themePref: p };
    const theme = apply(next);
    persist(next);
    set({ themePref: p, theme, activeSkin: resolveSkin(next.skin, next.customBg, theme).active });
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

  setSkin(id) {
    const preset = presetById(id);
    // 选皮肤顺带把明暗切到它的方向：皮肤与明暗是两个正交概念，
    // 但错配（深色皮肤 + 浅色主题）没有意义，这里替用户对齐，省一次操作。
    const themePref: ThemePref = preset ? preset.tone : get().themePref;
    const next = { ...pick(get()), skin: id, themePref };
    const theme = apply(next);
    persist(next);
    set({ skin: id, themePref, theme, activeSkin: resolveSkin(id, next.customBg, theme).active });
  },

  setCustomBg(hex) {
    const themePref: ThemePref = isDarkColor(hex) ? "dark" : "light";
    const next = { ...pick(get()), skin: CUSTOM_SKIN_ID, customBg: hex, themePref };
    const theme = apply(next);
    persist(next);
    set({
      skin: CUSTOM_SKIN_ID,
      customBg: hex,
      themePref,
      theme,
      activeSkin: resolveSkin(CUSTOM_SKIN_ID, hex, theme).active,
    });
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
    set({ theme, activeSkin: resolveSkin(get().skin, get().customBg, theme).active });

    // 挂系统主题监听：只有 auto 模式才跟随，显式选择后不打扰用户
    if (!mq) {
      try {
        mq = window.matchMedia(DARK_QUERY);
        mqHandler = () => {
          if (get().themePref !== "auto") return;
          const next = apply(pick(get()));
          set({ theme: next, activeSkin: resolveSkin(get().skin, get().customBg, next).active });
        };
        mq.addEventListener("change", mqHandler);
      } catch {
        /* 环境不支持则忽略，退化为静态主题 */
      }
    }
  },
}));

