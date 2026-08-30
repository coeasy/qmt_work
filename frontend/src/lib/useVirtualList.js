// G11-4 轻量虚拟列表 hook（无第三方依赖，选股结果/成分股等 5000+ 行场景）。
//
// 用法：
//   const vl = useVirtualList(items, { itemHeight: 28, overscan: 8 });
//   <div style={{ height: 420, overflow: "auto" }} onScroll={vl.onScroll}>
//     <div style={{ height: vl.totalHeight, position: "relative" }}>
//       {vl.visible.map((it, i) => (
//         <div key={it.code} style={{ position: "absolute", top: (vl.startIndex + i) * 28, height: 28, left: 0, right: 0 }}>
//           ...
//         </div>
//       ))}
//     </div>
//   </div>
import { useEffect, useRef, useState } from "react";

// 纯函数：窗口计算（可脱离 React 测试）
export function computeVirtualWindow(scrollTop, viewH, itemHeight, overscan, total) {
  const h = itemHeight > 0 ? itemHeight : 1;
  const startIndex = Math.max(0, Math.floor(scrollTop / h) - overscan);
  const endIndex = Math.min(total, Math.ceil((scrollTop + viewH) / h) + overscan);
  return {
    startIndex,
    endIndex,
    totalHeight: total * h,
    offsetY: startIndex * h,
  };
}

export function useVirtualList(items, { itemHeight = 28, overscan = 8, height = 0 } = {}) {
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(height || 0);
  const containerRef = useRef(null);

  useEffect(() => {
    if (height > 0) { setViewH(height); return; }          // 显式高度（测试/固定布局）
    const el = containerRef.current;
    if (!el) return;
    const measure = () => setViewH(el.clientHeight || 0);
    measure();
    const ro = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(measure) : null;
    if (ro) ro.observe(el);
    return () => { if (ro) ro.disconnect(); };
  }, [height]);

  // 数据/高度变化时钳制滚动位置
  useEffect(() => {
    const max = Math.max(0, items.length * itemHeight - viewH);
    if (scrollTop > max) setScrollTop(max);
  }, [items.length, itemHeight, viewH, scrollTop]);

  const win = computeVirtualWindow(scrollTop, viewH, itemHeight, overscan, items.length);
  return {
    visible: items.slice(win.startIndex, win.endIndex),
    startIndex: win.startIndex,
    totalHeight: win.totalHeight,
    offsetY: win.offsetY,
    onScroll: (e) => setScrollTop(e.currentTarget.scrollTop),
    containerRef,
  };
}
