import { useEffect, useRef, useState } from "react";
import { MENU, PAGES, pageLabel } from "@/app/routes";
import { useWorkspaceStore } from "@/stores/workspace";
import { marketApi } from "@/services/api";
import { normalizeCode } from "@/shared/format";
import type { Instrument } from "@/shared/types";
import s from "./shell.module.css";

/**
 * 顶部菜单栏（嵌在自绘标题栏内，见 shell/TitleBar.tsx）。
 *
 * 设计决策（方案 §4.4）：功能树不放左侧，改为顶部「一级菜单 = 业务域 + 下拉二级 = 域内页」。
 * 配套的快捷跳转（代码直达 / 命令面板 / 快捷键）在同批交付，
 * 否则高频操作会比侧边栏更慢 —— 这是顶部菜单能成立的前提。
 *
 * 品牌字样由 TitleBar 负责渲染，本组件只出菜单项与全局搜索，
 * 避免同一行里出现两个「qmt_work」。
 */
export function MenuBar() {
  const [openKey, setOpenKey] = useState<string>("");
  const open = useWorkspaceStore((st) => st.open);
  const barRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!openKey) return;
    const onDown = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setOpenKey("");
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [openKey]);

  const go = (key: string) => {
    open(key, {}, { title: pageLabel(key) });
    setOpenKey("");
  };

  return (
    <div className={s.menubar} ref={barRef}>
      {MENU.map((g) => (
        <div key={g.key} style={{ position: "relative", height: "100%" }}>
          <button
            type="button"
            className={[s.menuItem, openKey === g.key ? s.menuItemActive : ""]
              .filter(Boolean)
              .join(" ")}
            onClick={() => setOpenKey(openKey === g.key ? "" : g.key)}
            onMouseEnter={() => openKey && setOpenKey(g.key)}
          >
            {g.label}
          </button>
          {openKey === g.key && (
            <div className={s.menuDrop} role="menu">
              {g.items.map((k) => {
                const def = PAGES[k];
                if (!def) return null;
                return (
                  <button
                    key={k}
                    type="button"
                    role="menuitem"
                    className={s.menuDropItem}
                    onClick={() => go(k)}
                  >
                    <span>{def.label}</span>
                    {def.status === "planned" && (
                      <span style={{ marginLeft: "auto", color: "var(--text-faint)", fontSize: 11 }}>
                        待实现
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      ))}

      <span className={s.menuSpacer} />
      <GlobalSearch />
    </div>
  );
}

/**
 * 全局搜索 / 代码直达。
 * 输入 6 位代码回车 → 直接打开 K 线分析；输入文字 → 检索标的。
 */
function GlobalSearch() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Instrument[]>([]);
  const [active, setActive] = useState(0);
  const [focused, setFocused] = useState(false);
  const open = useWorkspaceStore((st) => st.open);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    const text = q.trim();
    if (text.length < 1 || /^\d{6}$/.test(text)) {
      setItems([]);
      return;
    }
    timer.current = window.setTimeout(() => {
      void marketApi
        .search(text, 8)
        .then((res) => {
          setItems(res ?? []);
          setActive(0);
        })
        .catch(() => setItems([]));
    }, 220);
    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, [q]);

  const openCode = (code: string, name?: string) => {
    const c = normalizeCode(code);
    open("quote", { code: c, name: name ?? "" }, { title: name ? `${name} ${c}` : c });
    setQ("");
    setItems([]);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      const text = q.trim();
      if (/^\d{6}$/.test(text)) {
        openCode(text);
        return;
      }
      const hit = items[active];
      if (hit) openCode(hit.code, hit.name);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, items.length - 1));
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    }
    if (e.key === "Escape") {
      setQ("");
      setItems([]);
    }
  };

  return (
    <div className={s.globalSearch}>
      <input
        className={s.globalSearchInput}
        placeholder="代码直达 / 搜索标的"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => setFocused(true)}
        onBlur={() => window.setTimeout(() => setFocused(false), 160)}
        aria-label="全局搜索"
      />
      {focused && items.length > 0 && (
        <div className={s.searchResults}>
          {items.map((it, i) => (
            <button
              key={it.code}
              type="button"
              className={[s.searchItem, i === active ? s.searchItemActive : ""]
                .filter(Boolean)
                .join(" ")}
              onMouseDown={(e) => {
                e.preventDefault();
                openCode(it.code, it.name);
              }}
            >
              <span className={s.searchCode}>{it.code}</span>
              <span>{it.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
