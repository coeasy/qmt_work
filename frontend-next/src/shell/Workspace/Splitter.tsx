import { useCallback, useState, type RefObject } from "react";
import s from "../shell.module.css";

export interface SplitterProps {
  dir: "h" | "v";
  /** 分栏容器引用，用于把指针位置换算成比例 */
  containerRef: RefObject<HTMLElement | null>;
  onRatio: (ratio: number) => void;
}

/**
 * 可拖拽分栏条。
 *
 * 旧前端分屏为固定 flex:1 等分且不可拖拽（styles.css:856-859）。
 * 本组件把指针位置换算为容器内比例，交给 workspace store 持久化。
 */
export function Splitter({ dir, containerRef, onRatio }: SplitterProps) {
  const [dragging, setDragging] = useState(false);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragging(true);

      const move = (ev: MouseEvent) => {
        const el = containerRef.current;
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const ratio =
          dir === "h"
            ? (ev.clientX - rect.left) / Math.max(rect.width, 1)
            : (ev.clientY - rect.top) / Math.max(rect.height, 1);
        onRatio(ratio);
      };

      const up = () => {
        setDragging(false);
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
      };

      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    },
    [containerRef, dir, onRatio],
  );

  return (
    <div
      role="separator"
      aria-orientation={dir === "h" ? "vertical" : "horizontal"}
      className={[
        s.splitter,
        dir === "h" ? s.splitterH : s.splitterV,
        dragging ? s.splitterDragging : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onMouseDown={onMouseDown}
    />
  );
}
