import { useEffect } from "react";
import { MENU } from "@/app/routes";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore, type PaneNode } from "@/stores/workspace";
import { nextPeriod } from "@/shared/periods";
import type { Period } from "@/shared/types";

function findLeaf(node: PaneNode | undefined, leafId: string): PaneNode | undefined {
  if (!node) return undefined;
  if (node.kind === "leaf") return node.id === leafId ? node : undefined;
  return findLeaf(node.a, leafId) ?? findLeaf(node.b, leafId);
}

/**
 * 全局快捷键。
 *
 * 对标通达信的关键效率来源：
 *   ⌘/Ctrl+K  命令面板（任意位置唤起）
 *   Alt+1..6  直达第 N 个业务域的首个页面
 *   F5        循环切换当前图表周期
 *   ⌘/Ctrl+B  显示/隐藏左侧数据面板
 * 代码直达（输入 6 位代码回车）在菜单栏搜索框与命令面板中均可用。
 */
export function Hotkeys() {
  const setCommandOpen = useUiStore((st) => st.setCommandOpen);
  const togglePanel = useUiStore((st) => st.toggleDataPanel);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        !!target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);

      // 命令面板：输入态下也允许
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCommandOpen(true);
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        togglePanel();
        return;
      }

      if (typing) return;

      // Alt+数字：直达业务域
      if (e.altKey && /^[1-9]$/.test(e.key)) {
        const idx = Number(e.key) - 1;
        const group = MENU[idx];
        const first = group?.items[0];
        if (first) {
          e.preventDefault();
          useWorkspaceStore.getState().open(first, {}, {});
        }
        return;
      }

      // F5：循环周期（作用于当前激活窗格）
      if (e.key === "F5") {
        e.preventDefault();
        const st = useWorkspaceStore.getState();
        const tab = st.tabs.find((t) => t.id === st.activeId);
        const leaf = findLeaf(tab?.tree, tab?.activeLeafId ?? "");
        if (!tab || !leaf || leaf.kind !== "leaf") return;
        const cur = (leaf.params.period as Period) ?? "1d";
        st.setLeafParams(tab.id, leaf.id, { ...leaf.params, period: nextPeriod(cur) });
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setCommandOpen, togglePanel]);

  return null;
}
