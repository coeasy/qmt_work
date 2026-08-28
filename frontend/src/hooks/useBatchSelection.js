import { useCallback, useEffect, useState } from "react";

// 通用批量选择状态：为列表接入「全选/单选/清空」，列表刷新后自动剔除已不存在的条目。
// 用法：
//   const bsel = useBatchSelection(records, "id");
//   表头：<input type="checkbox" checked={bsel.allSelected} onChange={bsel.toggleAll} />
//   每行：<input type="checkbox" checked={bsel.sel.has(r.id)} onChange={() => bsel.toggleOne(r.id)} />
//   删除条：<BatchDeleteBar count={bsel.selected.length} onDelete={() => del(bsel.selected)} onClear={bsel.clear} />
export function useBatchSelection(items, idKey = "id") {
  const [sel, setSel] = useState(() => new Set());

  // 列表刷新后移除已消失的选中项，防止残留 key 越界删除
  useEffect(() => {
    setSel((prev) => {
      if (prev.size === 0) return prev;
      const valid = new Set((items || []).map((it) => it[idKey]));
      const next = new Set([...prev].filter((id) => valid.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [items, idKey]);

  const arr = items || [];
  const allSelected = arr.length > 0 && arr.every((it) => sel.has(it[idKey]));

  const toggleOne = useCallback((id) => {
    setSel((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setSel((prev) =>
      prev.size === arr.length && arr.length > 0 ? new Set() : new Set(arr.map((it) => it[idKey])));
  }, [arr, idKey]);

  const clear = useCallback(() => setSel(new Set()), []);

  return {
    selected: [...sel],
    sel,
    toggleOne,
    toggleAll,
    clear,
    allSelected,
    hasSelected: sel.size > 0,
  };
}