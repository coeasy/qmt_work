import { useEffect, useMemo, useRef, useState } from "react";
import { allPages, MENU } from "@/app/routes";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import { normalizeCode } from "@/shared/format";
import s from "./shell.module.css";

interface Item {
  key: string;
  label: string;
  group: string;
  run: () => void;
}

/**
 * 命令面板（⌘K / Ctrl+K）。
 *
 * 顶部菜单方案成立的**前提组件**：菜单负责发现，命令面板负责效率。
 * 没有它，高频切换会比左侧功能树更慢。
 */
export function CommandPalette() {
  const openFlag = useUiStore((st) => st.commandOpen);
  const setOpen = useUiStore((st) => st.setCommandOpen);
  const openPage = useWorkspaceStore((st) => st.open);
  const closeTab = useWorkspaceStore((st) => st.close);
  const activeId = useWorkspaceStore((st) => st.activeId);

  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const items = useMemo<Item[]>(() => {
    const pageItems: Item[] = allPages().map((p) => {
      const group = MENU.find((g) => g.items.includes(p.key))?.label ?? "其他";
      return {
        key: `page:${p.key}`,
        label: p.label,
        group,
        run: () => openPage(p.key, {}, { title: p.label }),
      };
    });

    const actions: Item[] = [
      {
        key: "action:close-tab",
        label: "关闭当前窗口",
        group: "操作",
        run: () => activeId && closeTab(activeId),
      },
      {
        key: "action:quote",
        label: "打开 K 线分析（默认标的）",
        group: "操作",
        run: () => openPage("quote", { code: "000001.SZ" }, { title: "000001.SZ" }),
      },
      {
        key: "action:brokers",
        label: "打开连接管理",
        group: "操作",
        run: () => openPage("brokers", {}, { title: "连接管理" }),
      },
    ];

    return [...pageItems, ...actions];
  }, [openPage, closeTab, activeId]);

  const filtered = useMemo(() => {
    const text = q.trim().toLowerCase();
    if (!text) return items;
    return items.filter(
      (it) =>
        it.label.toLowerCase().includes(text) || it.group.toLowerCase().includes(text),
    );
  }, [items, q]);

  useEffect(() => {
    if (openFlag) {
      setQ("");
      setActive(0);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [openFlag]);

  if (!openFlag) return null;

  const run = (it: Item) => {
    it.run();
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, filtered.length - 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter") {
      const text = q.trim();
      // 6 位代码直达优先于列表项
      if (/^\d{6}$/.test(text)) {
        const code = normalizeCode(text);
        openPage("quote", { code }, { title: code });
        setOpen(false);
        return;
      }
      const hit = filtered[active];
      if (hit) run(hit);
    }
  };

  return (
    <div
      className={s.paletteBackdrop}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) setOpen(false);
      }}
    >
      <div className={s.palette} role="dialog" aria-modal="true" aria-label="命令面板">
        <input
          ref={inputRef}
          className={s.paletteInput}
          placeholder="搜索页面、执行操作，或输入 6 位代码直达…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
        />
        <div className={s.paletteList}>
          {filtered.length === 0 ? (
            <div className={s.paletteEmpty}>无匹配项</div>
          ) : (
            filtered.map((it, i) => (
              <button
                key={it.key}
                type="button"
                className={[s.paletteItem, i === active ? s.paletteItemActive : ""]
                  .filter(Boolean)
                  .join(" ")}
                onMouseEnter={() => setActive(i)}
                onClick={() => run(it)}
              >
                <span>{it.label}</span>
                <span className={s.paletteGroup}>{it.group}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
