import { Suspense, useRef } from "react";
import { PAGES, type PageProps } from "@/app/routes";
import { useWorkspaceStore, type PaneNode } from "@/stores/workspace";
import { Spinner } from "@/design/primitives";
import { Splitter } from "./Splitter";
import s from "../shell.module.css";

/**
 * 分栏树渲染器。
 * 递归渲染 split/leaf；叶子内挂载真正的页面组件。
 */
export function PaneTree({
  tabId,
  node,
  activeLeafId,
}: {
  tabId: string;
  node: PaneNode;
  activeLeafId: string;
}) {
  if (node.kind === "leaf") {
    return <PaneLeaf tabId={tabId} node={node} isActive={node.id === activeLeafId} />;
  }
  return <PaneSplit tabId={tabId} node={node} activeLeafId={activeLeafId} />;
}

function PaneSplit({
  tabId,
  node,
  activeLeafId,
}: {
  tabId: string;
  node: Extract<PaneNode, { kind: "split" }>;
  activeLeafId: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const setRatio = useWorkspaceStore((st) => st.setRatio);
  const isH = node.dir === "h";

  return (
    <div
      className={[s.paneSplit, isH ? s.paneSplitH : s.paneSplitV].join(" ")}
      ref={containerRef}
    >
      <div className={s.paneChild} style={{ flex: `${node.ratio} 1 0` }}>
        <PaneTree tabId={tabId} node={node.a} activeLeafId={activeLeafId} />
      </div>
      <Splitter
        dir={node.dir}
        containerRef={containerRef}
        onRatio={(r) => setRatio(tabId, node.id, r)}
      />
      <div className={s.paneChild} style={{ flex: `${1 - node.ratio} 1 0` }}>
        <PaneTree tabId={tabId} node={node.b} activeLeafId={activeLeafId} />
      </div>
    </div>
  );
}

function PaneLeaf({
  tabId,
  node,
  isActive,
}: {
  tabId: string;
  node: Extract<PaneNode, { kind: "leaf" }>;
  isActive: boolean;
}) {
  const setActiveLeaf = useWorkspaceStore((st) => st.setActiveLeaf);
  const splitLeaf = useWorkspaceStore((st) => st.splitLeaf);
  const closeLeaf = useWorkspaceStore((st) => st.closeLeaf);

  const def = PAGES[node.pageKey];
  const props: PageProps = { params: node.params, tabId, leafId: node.id };

  return (
    <div
      className={[s.paneLeaf, isActive ? s.paneLeafActive : ""].filter(Boolean).join(" ")}
      onMouseDown={() => setActiveLeaf(tabId, node.id)}
    >
      <div className={s.paneToolbar}>
        <span>{def?.label ?? node.pageKey}</span>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          className={s.paneToolBtn}
          title="向右分栏"
          onClick={() => splitLeaf(tabId, node.id, "h", node.pageKey)}
        >
          左右分栏
        </button>
        <button
          type="button"
          className={s.paneToolBtn}
          title="向下分栏"
          onClick={() => splitLeaf(tabId, node.id, "v", node.pageKey)}
        >
          上下分栏
        </button>
        <button
          type="button"
          className={s.paneToolBtn}
          title="关闭该窗格"
          onClick={() => closeLeaf(tabId, node.id)}
        >
          关闭
        </button>
      </div>

      <div className={def?.fullBleed ? s.paneBodyFlush : s.paneBody}>
        {def ? (
          <Suspense fallback={<Spinner label="加载页面…" />}>
            <def.comp {...props} />
          </Suspense>
        ) : (
          <div style={{ padding: 16, color: "var(--text-faint)" }}>
            未注册的页面：{node.pageKey}
          </div>
        )}
      </div>
    </div>
  );
}
