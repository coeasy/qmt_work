// G9 多图联动：图表规范（chart-spec）单一真源 + 跨页指标联动。
//
// - ChartConfigProvider：挂载时从 GET /market/chart-spec 拉取主/副图指标规范
//   （后端下发的选项/配色），经模块级缓存共享给所有图表页——消灭各页硬编码。
// - useChartSpec()：返回 { main, sub, candlestick, loading }。
// - 联动事件：任意页切换主/副图指标时 dispatchCustomEvent("qmt:chart:indicator",
//   { main, sub })，其它图表页（Boards/Etfs 迷你 K 线等）监听后同步渲染——
//   「在行情页切 MACD，板块页迷你图跟随」。
import { createContext, useContext, useEffect, useState } from "react";

import { api } from "../api.js";

const _specCache = { spec: null, ts: 0 };
const TTL = 5 * 60 * 1000;   // 5min（规范低频变化）

const ChartSpecContext = createContext({ spec: null, loading: false });

export function ChartConfigProvider({ children }) {
  const [spec, setSpec] = useState(_specCache.spec);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    if (_specCache.spec && Date.now() - _specCache.ts < TTL) {
      setSpec(_specCache.spec);
      return undefined;
    }
    setLoading(true);
    api.marketChartSpec()
      .then((d) => {
        if (!alive) return;
        _specCache.spec = d;
        _specCache.ts = Date.now();
        setSpec(d);
      })
      .catch(() => { /* 后端不可用 → 各页回退本地默认（chartPalette） */ })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, []);

  return (
    <ChartSpecContext.Provider value={{ spec, loading }}>
      {children}
    </ChartSpecContext.Provider>
  );
}

export function useChartSpec() {
  return useContext(ChartSpecContext);
}

// 指标切换联动事件（跨页广播）
export const CHART_INDICATOR_EVENT = "qmt:chart:indicator";

export function broadcastIndicatorChange(main, sub) {
  window.dispatchEvent(new CustomEvent(CHART_INDICATOR_EVENT, { detail: { main, sub } }));
}

// 监听联动事件（组件内 useEffect 调用，返回退订函数）
export function onIndicatorChange(handler) {
  const fn = (e) => handler(e.detail || {});
  window.addEventListener(CHART_INDICATOR_EVENT, fn);
  return () => window.removeEventListener(CHART_INDICATOR_EVENT, fn);
}

// 从 spec 取主/副图指标选项列表（含本地「无」回退）
export function mainIndicatorOptions(spec) {
  const opts = (spec?.main?.options || []).map((o) => ({ v: o.indicator, label: o.label }));
  return [{ v: "none", label: "无" }, ...opts];
}

export function subIndicatorOptions(spec) {
  return (spec?.sub?.options || []).map((o) => ({ v: o.indicator, label: o.label }));
}
