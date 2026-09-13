// Platform-First context: research/data/runtime are available independently of
// an optional trading connector. BrokerContext remains a compatibility surface
// for existing screens and is intentionally nested into this platform view.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useBroker } from "./BrokerContext.jsx";
import { api } from "./api.js";

const PlatformCtx = createContext(null);

const BASE_CAPABILITIES = {
  research: true, data: true, factors: true, backtest: true, paper: true,
  trading: false, realtimeTrading: false,
};

// fail-closed 兜底上下文：组件在 PlatformProvider 之外渲染时使用。
// 语义铁律：交易类能力一律「不可用」（绝不因兜底而误开放真实下单），
// 研究/数据类保持可用（与 BASE_CAPABILITIES / D12「无券商也能选股」契约对齐），
// 且组件不再因缺少上下文而白屏崩溃。
export const FALLBACK_CAPABILITIES = Object.freeze({ ...BASE_CAPABILITIES });

export const FALLBACK_PLATFORM = Object.freeze({
  ready: false,
  manifest: null,
  platform: null,
  capabilities: FALLBACK_CAPABILITIES,
  // 未知能力键 → undefined → false（fail-closed）；已知的研究/数据类保持 true（D12：无券商也能选股）
  can: (capability) => Boolean(FALLBACK_CAPABILITIES[capability]),
  refresh: () => Promise.resolve(),
  connectorReady: false,
  activeConnectionId: null,
  screeningReady: false,
  screeningProviders: Object.freeze([]),
});

let warnedMissingProvider = false;

export function PlatformProvider({ children }) {
  const broker = useBroker();
  const [manifest, setManifest] = useState(null);
  const [platform, setPlatform] = useState(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const [cap, status] = await Promise.all([
      api.capabilitiesSummary().catch(() => null),
      api.platformStatus().catch(() => null),
    ]);
    setManifest(cap || {});
    setPlatform(status || null);
    setReady(true);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const capabilities = useMemo(() => {
    const connected = broker.connectedCount > 0;
    // D-C 修复：优先采用后端 platform/status 的真实交易可用性，而非恒 false 的硬编码。
    const platCaps = platform?.capabilities || {};
    const declared = manifest?.capabilities || manifest?.data_capabilities || {};
    return {
      ...BASE_CAPABILITIES,
      ...declared,
      ...platCaps,
      trading: Boolean(platCaps.trading && connected),
      realtimeTrading: Boolean(platCaps.realtimeTrading && connected),
      connector: connected,
    };
  }, [broker.connectedCount, manifest, platform]);

  const screeningReady = Boolean(platform?.sources?.screening_ready);
  const screeningProviders = platform?.sources?.screening_providers || [];

  const can = useCallback((capability) => Boolean(capabilities[capability]), [capabilities]);
  const value = useMemo(() => ({
    ready, manifest, platform, capabilities, can, refresh,
    connectorReady: broker.connectedCount > 0,
    activeConnectionId: broker.activeId,
    // D12：未连接券商时选股仍应可用（只要有 eltdx/baostock/akshare/本地数据）
    screeningReady,
    screeningProviders,
  }), [ready, manifest, platform, capabilities, can, refresh, broker.connectedCount, broker.activeId, screeningReady, screeningProviders]);

  return <PlatformCtx.Provider value={value}>{children}</PlatformCtx.Provider>;
}

// 在 Provider 之外调用时返回 fail-closed 兜底上下文（不抛异常，避免整页白屏）。
// 开发态只提示一次，便于发现漏挂 Provider 的接线问题；生产态静默降级。
export function usePlatform() {
  const value = useContext(PlatformCtx);
  if (value) return value;
  if (!warnedMissingProvider) {
    warnedMissingProvider = true;
    if (import.meta?.env?.DEV) {
      console.warn(
        "[PlatformContext] usePlatform 在 PlatformProvider 之外调用，已回退 fail-closed 能力集（交易能力不可用）。",
      );
    }
  }
  return FALLBACK_PLATFORM;
}

