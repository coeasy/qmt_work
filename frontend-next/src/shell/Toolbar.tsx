import { PAGES } from "@/app/routes";
import { useWorkspaceStore } from "@/stores/workspace";
import { PERIODS, PERIOD_LABELS } from "@/shared/periods";
import type { Period } from "@/shared/types";
import s from "./shell.module.css";

const INDICATORS = ["MA", "BOLL", "SAR"];

/**
 * 上下文工具条（通达信式）：作用于当前激活窗格。
 *
 * 与菜单栏的分工：菜单负责「打开什么」，工具条负责「当前窗格怎么显示」。
 */
export function Toolbar() {
  const tabs = useWorkspaceStore((st) => st.tabs);
  const activeId = useWorkspaceStore((st) => st.activeId);
  const setLeafParams = useWorkspaceStore((st) => st.setLeafParams);
  const splitLeaf = useWorkspaceStore((st) => st.splitLeaf);
  const open = useWorkspaceStore((st) => st.open);

  const tab = tabs.find((t) => t.id === activeId);
  const leaf = findLeaf(tab?.tree, tab?.activeLeafId ?? "");
  const pageKey = leaf?.pageKey ?? "";
  const params = leaf?.params ?? {};
  const def = PAGES[pageKey];

  const isChart = pageKey === "quote" || pageKey === "minutes";
  const period = (params.period as Period) ?? "1d";
  const indicators = (params.indicators as string[]) ?? ["MA"];
  const linked = params.linked !== false;

  const patch = (p: Record<string, unknown>) => {
    if (!tab || !leaf) return;
    setLeafParams(tab.id, leaf.id, { ...params, ...p });
  };

  return (
    <div className={s.toolbar}>
      <div className={s.toolGroup}>
        <span className={s.toolLabel}>当前</span>
        <span style={{ color: "var(--text)", fontSize: "var(--font-sm)" }}>
          {def?.label ?? "—"}
        </span>
      </div>

      {isChart && (
        <>
          <div className={s.toolGroup}>
            <span className={s.toolLabel}>周期</span>
            {PERIODS.map((p) => (
              <button
                key={p}
                type="button"
                className={[s.toolBtn, p === period ? s.toolBtnActive : ""]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => patch({ period: p })}
              >
                {PERIOD_LABELS[p]}
              </button>
            ))}
          </div>

          <div className={s.toolGroup}>
            <span className={s.toolLabel}>主图</span>
            {INDICATORS.map((ind) => {
              const on = indicators.includes(ind);
              return (
                <button
                  key={ind}
                  type="button"
                  className={[s.toolBtn, on ? s.toolBtnActive : ""].filter(Boolean).join(" ")}
                  onClick={() =>
                    patch({
                      indicators: on
                        ? indicators.filter((x) => x !== ind)
                        : [...indicators, ind],
                    })
                  }
                >
                  {ind}
                </button>
              );
            })}
          </div>

          <div className={s.toolGroup}>
            <span className={s.toolLabel}>联动</span>
            <button
              type="button"
              className={[s.toolBtn, linked ? s.toolBtnActive : ""].filter(Boolean).join(" ")}
              title="同组图表十字光标同步（多周期对齐）"
              onClick={() => patch({ linked: !linked })}
            >
              {linked ? "已开启" : "已关闭"}
            </button>
          </div>
        </>
      )}

      <div className={s.toolGroup}>
        <span className={s.toolLabel}>分栏</span>
        <button
          type="button"
          className={s.toolBtn}
          onClick={() => tab && leaf && splitLeaf(tab.id, leaf.id, "h", leaf.pageKey)}
        >
          左右
        </button>
        <button
          type="button"
          className={s.toolBtn}
          onClick={() => tab && leaf && splitLeaf(tab.id, leaf.id, "v", leaf.pageKey)}
        >
          上下
        </button>
      </div>

      <div className={s.toolGroup}>
        <span className={s.toolLabel}>布局模板</span>
        <button
          type="button"
          className={s.toolBtn}
          onClick={() => open("quote", { period: "1d" }, { title: "K 线分析", reuse: "new" })}
        >
          单图
        </button>
        <button
          type="button"
          className={s.toolBtn}
          onClick={() =>
            open("quoteboard", {}, { title: "报价牌", reuse: "new" })
          }
        >
          报价牌
        </button>
      </div>
    </div>
  );
}

function findLeaf(
  node: import("@/stores/workspace").PaneNode | undefined,
  leafId: string,
): Extract<import("@/stores/workspace").PaneNode, { kind: "leaf" }> | undefined {
  if (!node) return undefined;
  if (node.kind === "leaf") return node.id === leafId ? node : undefined;
  return findLeaf(node.a, leafId) ?? findLeaf(node.b, leafId);
}
