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
 *   ⌘/Ctrl+W  关闭当前标签
 *   Ctrl+Tab  切换标签（Shift 反向）
 *   F11       全屏切换
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

      // 关标签 / 切标签：与浏览器一致，**输入态下也生效**（否则在搜索框里按 Ctrl+W
      // 会走浏览器默认行为，而桌面壳内那是「关掉整个应用窗口」）。
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "w") {
        e.preventDefault();
        const st = useWorkspaceStore.getState();
        if (st.activeId) st.close(st.activeId);
        return;
      }

      if (e.ctrlKey && e.key === "Tab") {
        e.preventDefault();
        const st = useWorkspaceStore.getState();
        const idx = st.tabs.findIndex((t) => t.id === st.activeId);
        if (idx < 0 || st.tabs.length < 2) return;
        const step = e.shiftKey ? -1 : 1;
        const next = st.tabs[(idx + step + st.tabs.length) % st.tabs.length];
        if (next) st.activate(next.id);
        return;
      }

      if (typing) return;

      // F11：全屏。用 Fullscreen API 而不是自建 IPC —— Electron 原生支持，
      // 且无需为「窗口是否全屏」再维护一份主进程/渲染进程同步状态。
      if (e.key === "F11") {
        e.preventDefault();
        toggleFullscreen();
        return;
      }

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

/** 全屏切换；环境不支持 Fullscreen API 时静默忽略（jsdom / 老旧内核）。 */
function toggleFullscreen(): void {
  try {
    if (document.fullscreenElement) {
      void document.exitFullscreen?.();
      return;
    }
    void document.documentElement.requestFullscreen?.();
  } catch {
    /* 忽略：全屏只是便利功能 */
  }
}
