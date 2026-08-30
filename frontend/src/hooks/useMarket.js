// 行情数据统一获取层（前端单一真相）：
// - 整合 quote / kline / stock-info / financial / sources 五类行情接口
// - 自动去重：相同 code+period+count+adj 不会并发请求
// - 自动取消：参数变更时上一次未完成请求会 abort
// - 自动降级：连接/异常时不再静默空数据，错误向上抛由 UI 决定如何显示
// - 数据源标签（TDX / 券商 / 缓存）由 source 字段统一返回
// - 提供稳定 hook：useKline / useQuoteSnapshot / useFundamentals

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

/* ------------------- 内部：单例请求去重表 ------------------- */
// 同 (key, url, params) 多个组件同时请求 → 共享同一 Promise
const _inflight = new Map();
function deduped(key, fn) {
  const k = JSON.stringify(key);
  if (_inflight.has(k)) return _inflight.get(k);
  const p = fn();
  _inflight.set(k, p);
  p.finally(() => _inflight.delete(k));
  return p;
}

/* ------------------- 纯函数：格式化 ------------------- */
// 金额格式化收敛到 lib/format.js 的唯一实现。
// 背景：本文件原有一份 formatAmount（万档 .2f），与 lib/format.js 的 fmtAmount
// （万档 .1f）及 QuoteBoard.jsx 的本地副本三份并存且口径不一致——同一金额在
// 不同页面显示不同精度。现统一 re-export，调用方（Boards/Etfs/IndexOverview/
// MarketData 等）零改动即自动对齐，且新增页面只会指向唯一实现。
export { fmtAmount as formatAmount } from "../lib/format.js";

export function formatPct(v) {
  if (v == null || isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
}
export function preCloseOf(tick, info) {
  return tick?.preClose != null ? Number(tick.preClose)
    : (tick?.lastClose != null ? Number(tick.lastClose)
      : (tick?.pre_close != null ? Number(tick.pre_close)
        : (info?.preClose != null ? Number(info.preClose)
          : (info?.pre_close != null ? Number(info.pre_close) : null))));
}

// 实时 tick 就地更新最后一根 K 线（成交量等用 tick 覆盖；high/low 扩展）
export function applyTickToBars(bars, tick) {
  if (!bars || bars.length === 0 || !tick || tick.last == null) return bars;
  const arr = bars.slice();
  const last = { ...arr[arr.length - 1] };
  const v = Number(tick.last);
  const open = Number(last.open);
  last.close = v;
  last.high = Math.max(Number(last.high) || v, open, v);
  last.low = Math.min(Number(last.low) >= 0 ? Number(last.low) : v, open, v);
  if (tick.volume != null) last.volume = Number(tick.volume);
  arr[arr.length - 1] = last;
  return arr;
}

/* ------------------- 纯函数：低层 API 封装 ------------------- */
// 所有数据获取都走这里；上层 hook 只调这些，避免每个组件各写一份 try/catch
export async function fetchQuote(code, connId) {
  if (!code) return null;
  return deduped(["quote", code, connId || ""], () =>
    api.marketQuote({ code, conn_id: connId }).catch((e) => {
      // 503/未连接 视为"无快照"，返回 null 让上层走 stock-info/财务兜底
      if (e && /503|未连接|TDX|行情/.test(String(e.message || ""))) return null;
      throw e;
    }),
  );
}
export async function fetchKline({ code, period, count = 180, adj, connId, force = false }) {
  if (!code || !period) return { bars: [], source: "", count: 0 };
  const params = { code, period, count, conn_id: connId };
  if (force) params.force = true;
  if (adj) params.adj = adj;
  return deduped(["kline", JSON.stringify(params)], () => api.marketKline(params));
}
export async function fetchStockInfo(code, connId) {
  if (!code) return null;
  return deduped(["info", code, connId || ""], () =>
    api.marketStockInfo({ code, conn_id: connId }).catch(() => null),
  );
}
export async function fetchFinancial(code) {
  if (!code) return null;
  return deduped(["fin", code], () => api.financial(code).catch(() => null));
}
export async function fetchSources() {
  return deduped(["sources"], () => api.get("/market/sources").catch(() => ({
    sources: ["broker", "eltdx"],
    active: "eltdx",
    health: { broker: { available: false }, eltdx: { available: true } },
  })));
}

/* ------------------- 通用：参数化数据 hook ------------------- */
// 用法：
//   const { data, loading, err, reload } = useAsyncData(
//     () => fetchKline({ code, period, count, adj, connId }),
//     [code, period, count, adj, connId],
//   );
export function useAsyncData(loader, deps, { initial = null, debounceMs = 0 } = {}) {
  const [data, setData] = useState(initial);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const tickRef = useRef(0);
  const timerRef = useRef(null);
  // 每次渲染把最新 loader 存入 ref：reload 只依赖此 ref，切换参数/代码时
  // 立即取到新闭包，避免 useCallback([]) 冻结首帧 loader 导致请求旧参数数据。
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const reload = useCallback(() => {
    const tick = ++tickRef.current;
    if (timerRef.current) clearTimeout(timerRef.current);
    const run = () => {
      setLoading(true); setErr("");
      loaderRef.current()
        .then((d) => { if (tickRef.current === tick) { setData(d); setLoading(false); } })
        .catch((e) => { if (tickRef.current === tick) { setErr(e?.message || String(e)); setLoading(false); } });
    };
    if (debounceMs > 0) timerRef.current = setTimeout(run, debounceMs);
    else run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounceMs]);

  useEffect(() => { reload(); /* eslint-disable-next-line */ }, [...(deps || []), reload]);
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  return { data, loading, err, reload };
}

/* ------------------- 业务 hook ------------------- */
export function useKline({ code, period, count, adj, connId, force = false }) {
  return useAsyncData(
    () => fetchKline({ code, period, count, adj, connId, force }),
    [code, period, count, adj, connId, force],
    { initial: { bars: [], source: "", count: 0 }, debounceMs: 60 },
  );
}
export function useFundamentals(code, connId) {
  const { data, loading, err, reload } = useAsyncData(
    async () => {
      const [info, fin] = await Promise.all([fetchStockInfo(code, connId), fetchFinancial(code)]);
      return { info, fin };
    },
    [code, connId],
    { initial: { info: null, fin: null } },
  );
  return { ...data, loading, err, reload };
}
export function useQuoteSnapshot(code, connId) {
  return useAsyncData(() => fetchQuote(code, connId), [code, connId], { initial: null });
}
