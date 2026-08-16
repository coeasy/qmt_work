import { useEffect, useRef } from "react";
import * as echarts from "echarts";

// 轻量 ECharts 封装：option 变化时重建/更新；容器自适应。
export default function Chart({ option, height = 320, className = "" }) {
  const ref = useRef(null);
  const inst = useRef(null);

  useEffect(() => {
    if (!ref.current) return;
    if (!inst.current) inst.current = echarts.init(ref.current, "dark");
    inst.current.setOption(option, true);
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

  return <div ref={ref} className={`chart-box ${className}`} style={{ height }} />;
}
