import { create } from "zustand";

/**
 * 自选股 store。
 * 分组能力（对标通达信的自选股分类）留作后续扩展，当前为单组。
 */

const STORAGE_KEY = "qmt.watchlist.v1";
const DEFAULT_CODES = ["000001.SZ", "600519.SH", "000300.SH", "399006.SZ"];

function load(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_CODES;
    const arr = JSON.parse(raw) as unknown;
    if (!Array.isArray(arr)) return DEFAULT_CODES;
    return arr.filter((x): x is string => typeof x === "string");
  } catch {
    return DEFAULT_CODES;
  }
}

function save(codes: string[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(codes));
  } catch {
    /* 忽略 */
  }
}

interface WatchlistState {
  codes: string[];
  add: (code: string) => void;
  remove: (code: string) => void;
  toggle: (code: string) => void;
  setAll: (codes: string[]) => void;
  has: (code: string) => boolean;
}

export const useWatchlistStore = create<WatchlistState>((set, get) => ({
  codes: load(),

  add(code) {
    if (get().codes.includes(code)) return;
    const next = [...get().codes, code];
    save(next);
    set({ codes: next });
  },

  remove(code) {
    const next = get().codes.filter((c) => c !== code);
    save(next);
    set({ codes: next });
  },

  toggle(code) {
    if (get().codes.includes(code)) get().remove(code);
    else get().add(code);
  },

  setAll(codes) {
    save(codes);
    set({ codes });
  },

  has(code) {
    return get().codes.includes(code);
  },
}));
