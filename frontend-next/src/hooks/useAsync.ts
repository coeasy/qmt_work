import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export interface UseAsyncResult<T> extends AsyncState<T> {
  /** 手动重新拉取（用于「刷新」按钮与写操作后回读） */
  reload: () => Promise<void>;
  /** 本地覆盖数据（写操作后的乐观更新） */
  set: (next: T | null) => void;
}

/**
 * 统一的数据拉取 hook。
 *
 * 设计要点：
 * - 零 mock：失败时保留 error 文案，绝不返回假数据充当成功。
 * - 竞态安全：以自增 seq 丢弃过期响应，避免快速切换标的时旧响应覆盖新响应。
 * - 卸载安全：组件卸载后不再 setState。
 *
 * @param fetcher 拉取函数；依赖变化时应通过 deps 传入
 * @param deps    依赖数组（如 [code, period]）
 */
export function useAsync<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
): UseAsyncResult<T> {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    loading: true,
    error: null,
  });

  const seqRef = useRef(0);
  const aliveRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const run = useCallback(async () => {
    const seq = ++seqRef.current;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await fetcherRef.current();
      if (!aliveRef.current || seq !== seqRef.current) return;
      setState({ data, loading: false, error: null });
    } catch (e) {
      if (!aliveRef.current || seq !== seqRef.current) return;
      const msg = e instanceof Error ? e.message : String(e);
      setState({ data: null, loading: false, error: msg });
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void run();
    return () => {
      aliveRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { ...state, reload: run, set: (next) => setState((p) => ({ ...p, data: next })) };
}
