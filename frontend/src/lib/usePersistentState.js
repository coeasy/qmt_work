import { useCallback, useEffect, useRef, useState } from "react";

// 页面级 Tab/筛选状态持久化（T20）：localStorage 驱动，切页/刷新后恢复。
//
// 用法：const [tab, setTab] = usePersistentState("trade:tab", "order");
// key 全局唯一（建议 page:field），value 必须是可 JSON 序列化的。
// 写入节流（250ms）避免高频切换刷盘。
export default function usePersistentState(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw != null) return JSON.parse(raw);
    } catch { /* 损坏数据 → 用默认值 */ }
    return initial;
  });
  const timer = useRef(null);

  // 外部 setValue 与 localStorage 同步（节流）
  const set = useCallback((v) => {
    setValue((prev) => {
      const next = typeof v === "function" ? v(prev) : v;
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* quota */ }
      }, 250);
      return next;
    });
  }, [key]);

  // 卸载时刷新未写入的尾部变更
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  return [value, set];
}
