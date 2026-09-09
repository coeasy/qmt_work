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

export function PlatformProvider({ children }) {
  const broker = useBroker();
  const [manifest, setManifest] = useState(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const data = await api.capabilitiesSummary().catch(() => null);
    setManifest(data || {});
    setReady(true);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const capabilities = useMemo(() => {
    const connected = broker.connectedCount > 0;
    const declared = manifest?.capabilities || manifest?.data_capabilities || {};
    return {
      ...BASE_CAPABILITIES,
      ...declared,
      trading: Boolean(declared.trading && connected),
      realtimeTrading: Boolean(declared.realtimeTrading && connected),
      connector: connected,
    };
  }, [broker.connectedCount, manifest]);

  const can = useCallback((capability) => Boolean(capabilities[capability]), [capabilities]);
  const value = useMemo(() => ({
    ready, manifest, capabilities, can, refresh,
    connectorReady: broker.connectedCount > 0,
    activeConnectionId: broker.activeId,
  }), [ready, manifest, capabilities, can, refresh, broker.connectedCount, broker.activeId]);

  return <PlatformCtx.Provider value={value}>{children}</PlatformCtx.Provider>;
}

export function usePlatform() {
  const value = useContext(PlatformCtx);
  if (!value) throw new Error("usePlatform 必须在 PlatformProvider 内使用");
  return value;
}

