import { useEffect, useState } from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import s from "../shell.module.css";

/**
 * 工作区 Tab 栏。
 * 支持点击切换、中键/按钮关闭、拖拽排序、**右键菜单**（关闭/关闭其他/关闭右侧）。
 * 「已逐出（超出 LRU 上限）」的 Tab 以半透明标识，提示其 UI 状态已释放。
 *
 * 右键菜单是行情软件的必备操作：盯盘时常常一口气开十几个 Tab，
 * 逐个去点那个小 × 太慢，而「关闭右侧」是最常用的收尾动作。
 */
export function TabBar({ aliveIds }: { aliveIds: string[] }) {
  const tabs = useWorkspaceStore((st) => st.tabs);
  const activeId = useWorkspaceStore((st) => st.activeId);
  const activate = useWorkspaceStore((st) => st.activate);
  const close = useWorkspaceStore((st) => st.close);
  const closeOthers = useWorkspaceStore((st) => st.closeOthers);
  const closeRight = useWorkspaceStore((st) => st.closeRight);
  const moveTab = useWorkspaceStore((st) => st.moveTab);

  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; tabId: string; idx: number } | null>(
    null,
  );

  // 点击别处 / 再右键 / 窗口失焦都关掉菜单
  useEffect(() => {
    if (!menu) return;
    const dismiss = () => setMenu(null);
    document.addEventListener("click", dismiss);
    document.addEventListener("contextmenu", dismiss);
    window.addEventListener("blur", dismiss);
    return () => {
      document.removeEventListener("click", dismiss);
      document.removeEventListener("contextmenu", dismiss);
      window.removeEventListener("blur", dismiss);
    };
  }, [menu]);

  const alive = new Set(aliveIds);

  return (
    <div className={s.tabbar} role="tablist">
      {tabs.map((t, i) => {
        const isActive = t.id === activeId;
        const isAlive = alive.has(t.id);
        return (
          <div
            key={t.id}
            role="tab"
            aria-selected={isActive}
            draggable
            className={[
              s.tabItem,
              isActive ? s.tabItemActive : "",
              isAlive ? "" : s.tabItemEvicted,
              overIdx === i && dragIdx !== null && dragIdx !== i ? s.tabItemActive : "",
            ]
              .filter(Boolean)
              .join(" ")}
            onClick={() => activate(t.id)}
            onAuxClick={(e) => {
              if (e.button === 1) {
                e.preventDefault();
                close(t.id);
              }
            }}
            onContextMenu={(e) => {
              // 阻止冒泡：否则会立刻触发 document 上的 dismiss 监听，菜单一闪就没
              e.preventDefault();
              e.stopPropagation();
              setMenu({ x: e.clientX, y: e.clientY, tabId: t.id, idx: i });
            }}
            onDragStart={() => setDragIdx(i)}
            onDragOver={(e) => {
              e.preventDefault();
              setOverIdx(i);
            }}
            onDragEnd={() => {
              if (dragIdx !== null && overIdx !== null && dragIdx !== overIdx) {
                moveTab(dragIdx, overIdx);
              }
              setDragIdx(null);
              setOverIdx(null);
            }}
            title={isAlive ? t.title : `${t.title}（已释放，点击重新加载）`}
          >
            <span className={s.tabTitle}>{t.title}</span>
            <button
              type="button"
              className={s.tabClose}
              aria-label={`关闭 ${t.title}`}
              onClick={(e) => {
                e.stopPropagation();
                close(t.id);
              }}
            >
              ×
            </button>
          </div>
        );
      })}

      {menu && (
        <div className={s.tabMenu} style={{ left: menu.x, top: menu.y }} role="menu">
          <button
            type="button"
            role="menuitem"
            className={s.tabMenuItem}
            onClick={() => {
              close(menu.tabId);
              setMenu(null);
            }}
          >
            关闭
          </button>
          <button
            type="button"
            role="menuitem"
            className={s.tabMenuItem}
            disabled={tabs.length <= 1}
            onClick={() => {
              closeOthers(menu.tabId);
              setMenu(null);
            }}
          >
            关闭其他
          </button>
          <button
            type="button"
            role="menuitem"
            className={s.tabMenuItem}
            disabled={menu.idx >= tabs.length - 1}
            onClick={() => {
              closeRight(menu.tabId);
              setMenu(null);
            }}
          >
            关闭右侧
          </button>
        </div>
      )}
    </div>
  );
}
