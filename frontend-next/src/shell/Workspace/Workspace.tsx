import { useEffect } from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import { PaneTree } from "./PaneTree";
import { TabBar } from "./TabBar";
import s from "../shell.module.css";

/**
 * 工作区。
 *
 * 渲染策略（对齐方案 §4.5 的 keep-alive 语义）：
 * - 仅渲染 LRU 前 MAX_ALIVE 个 Tab（超出者不挂载 → 释放 ECharts/klinecharts 实例与 DOM）
 * - 已挂载但非激活的 Tab 用 visibility:hidden 保留（尺寸不丢，切换无重排）
 * - 对比旧前端：旧实现非激活用 visibility 但 LRU 逐出即卸载且只恢复 params，
 *   本实现把「是否渲染」与「是否可见」显式分开，逐出行为可预期
 */
export function Workspace() {
  const tabs = useWorkspaceStore((st) => st.tabs);
  const activeId = useWorkspaceStore((st) => st.activeId);
  const aliveOrder = useWorkspaceStore((st) => st.aliveOrder);

  const aliveIds = aliveOrder.slice(0, 8);
  const aliveSet = new Set(aliveIds);
  const rendered = tabs.filter((t) => aliveSet.has(t.id));

  // 确保激活 Tab 一定在 LRU 头部（例如通过外部入口切到被逐出的 Tab）
  const activate = useWorkspaceStore((st) => st.activate);
  useEffect(() => {
    if (activeId && !aliveSet.has(activeId)) activate(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  return (
    <div className={s.workspace}>
      <TabBar aliveIds={aliveIds} />
      <div className={s.wsBody}>
        {rendered.map((t) => {
          const isActive = t.id === activeId;
          return (
            <div
              key={t.id}
              className={isActive ? s.shownTab : s.hiddenTab}
              aria-hidden={!isActive}
            >
              <PaneTree tabId={t.id} node={t.tree} activeLeafId={t.activeLeafId} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
