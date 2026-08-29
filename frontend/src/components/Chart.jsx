import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import * as echarts from "echarts";

// 轻量 ECharts 封装：option 变化时重建/更新；容器自适应；空 option 安全降级。
// 通过 ref 暴露 getInstance()，供父组件挂载原生事件（如十字光标 OHLC 信息条）。
const Chart = forwardRef(function Chart({ option, height = 320, className = "" }, ref) {
  const elRef = useRef(null);
  const inst = useRef(null);

  useImperativeHandle(ref, () => ({ getInstance: () => inst.current }), []);

  // 容器必须有显式高度与宽度（ECharts 才能正确 init）
  useEffect(() => {
    if (!elRef.current) return;
    const el = elRef.current;
    // 等下一个 tick 让父布局完成（避免 mount 时 width=0）
    const init = () => {
      try {
        if (!inst.current) inst.current = echarts.init(el, "dark");
        inst.current.resize();
      } catch (e) { console.warn("[Chart.init]", e); }
    };
    init();
    const ro = new ResizeObserver(init);
    ro.observe(el);
    return () => { ro.disconnect(); };
  }, []);

  useEffect(() => {
    if (!inst.current || !option) return;
    try { inst.current.setOption(option, true); } catch (e) { console.warn("[Chart.setOption]", e); }
  }, [option]);

  useEffect(() => {
    const onResize = () => inst.current && inst.current.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (inst.current) {
        inst.current.dispose();
        inst.current = null;
      }
    };
  }, []);

  return <div ref={elRef} className={`chart-box ${className}`} style={{ height, width: "100%" }} />;
});

export default Chart;
