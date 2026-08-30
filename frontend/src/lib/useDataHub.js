import { useEffect, useRef, useState } from "react";

import { peek, subscribe } from "./dataHub.js";

// G4 全量铺开：dataHub 便捷 hook。
// 把「订阅 → 状态」封装成一行：
//   const { data, stale, error, loading, invalidate } = useDataHub(
//     `market:limitup:${sector}`, () => api.marketLimitup({ sector, ... }), 30000);
//
// - 已有快照秒回（data 立即就位，stale 标记）；
// - 组件激活且数据过期时自动重拉（interval ms，0 = 不轮询）；
// - invalidate() 手动强制刷新；卸载自动退订。
export default function useDataHub(topic, fetcher, interval = 0, opts = {}) {
  const [state, setState] = useState(() => {
    const snap = peek(topic);
    return snap ? { data: snap.data, stale: false, error: null, loading: false } : { data: null, stale: false, error: null, loading: true };
  });
  const [nonce, setNonce] = useState(0);
  const unsubRef = useRef(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // 订阅：回调更新 state；触发一次拉取（已有快照由 subscribe 内部立即回调）
  useEffect(() => {
    let alive = true;
    const unsub = subscribe(topic, () => fetcherRef.current(), (r) => {
      if (!alive) return;
      if (r.error) {
        setState((s) => ({ ...s, error: r.error, loading: false }));
      } else {
        setState({ data: r.data, stale: r.stale, error: null, loading: false });
      }
    });
    unsubRef.current = unsub;
    return () => { alive = false; if (unsubRef.current) unsubRef.current(); unsubRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic, nonce]);

  // 可选轮询（激活时，dataHub 已做 min_interval 节流，这里仅兜底低频刷新）
  useEffect(() => {
    if (!interval || interval <= 0) return undefined;
    const id = setInterval(() => {
      const snap = peek(topic);
      if (!snap || Date.now() - snap.ts >= interval) {
        setNonce((n) => n + 1);   // 重新订阅 → 触发拉取
      }
    }, Math.max(interval, 5000));
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic, interval]);

  const invalidate = () => {
    import("./dataHub.js").then((m) => m.invalidate(topic));
    setNonce((n) => n + 1);
  };

  return { ...state, invalidate };
}
