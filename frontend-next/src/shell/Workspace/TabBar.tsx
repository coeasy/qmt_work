import { useState } from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import s from "../shell.module.css";

/**
 * 工作区 Tab 栏。
 * 支持点击切换、中键/按钮关闭、拖拽排序。
 * 「已逐出（超出 LRU 上限）」的 Tab 以半透明标识，提示其 UI 状态已释放。
 */
export function TabBar({ aliveIds }: { aliveIds: string[] }) {
  const tabs = useWorkspaceStore((st) => st.tabs);
  const activeId = useWorkspaceStore((st) => st.activeId);
  const activate = useWorkspaceStore((st) => st.activate);
  const close = useWorkspaceStore((st) => st.close);
  const moveTab = useWorkspaceStore((st) => st.moveTab);

  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);

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
    </div>
  );
}
