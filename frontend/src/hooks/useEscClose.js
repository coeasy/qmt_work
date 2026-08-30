import { useEffect } from "react";

// 面板 ESC 关闭（T18）：页面内面板/抽屉统一支持 ESC 关闭，避免只靠关闭按钮。
// 用法：useEscClose(active, onClose) —— active 为 true 时监听 ESC。
export default function useEscClose(active, onClose) {
  useEffect(() => {
    if (!active || typeof onClose !== "function") return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [active, onClose]);
}
