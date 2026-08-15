// 券商连接上下文：单一真相来源为后端 BrokerManager。
// 前端为纯 SPA，仅持有「当前活跃连接 conn_id」并透传给所有 REST 端点；
// 实时行情 WS 跟随后端 active 连接，因此切换连接 = 设为活跃。
import { createContext, useContext, useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

const BrokerCtx = createContext(null);

export function BrokerProvider({ children }) {
  const [profiles, setProfiles] = useState([]);
  const [brokers, setBrokers] = useState([]); // 连接状态列表
  const [activeId, setActiveId] = useState("");
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const [pr, br] = await Promise.all([
      api.brokerProfiles().catch(() => []),
      api.listBrokers().catch(() => []),
    ]);
    setProfiles(pr || []);
    const list = br || [];
    setBrokers(list);
    const act = list.find((b) => b.active);
    setActiveId(act ? act.conn_id : "");
    setReady(true);
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000); // 连接状态可能由后端自动重连变化
    return () => clearInterval(t);
  }, [refresh]);

  const setActive = useCallback(async (id) => {
    await api.setActiveBroker(id);
    await refresh();
  }, [refresh]);

  const connect = useCallback(async (id) => {
    await api.connectBroker(id);
    await refresh();
  }, [refresh]);

  const disconnect = useCallback(async (id) => {
    await api.disconnectBroker(id);
    await refresh();
  }, [refresh]);

  const remove = useCallback(async (id) => {
    await api.removeBroker(id);
    await refresh();
  }, [refresh]);

  const test = useCallback((cfg) => api.testBroker(cfg), []);

  const autoDetect = useCallback(() => api.autoDetectBrokers(), []);

  const add = useCallback(async (cfg) => {
    const r = await api.addBroker(cfg);
    await refresh();
    return r;
  }, [refresh]);

  const activeBroker = brokers.find((b) => b.conn_id === activeId) || null;
  const connectedCount = brokers.filter((b) => b.connected).length;

  return (
    <BrokerCtx.Provider value={{
      profiles, brokers, activeId, activeBroker, connectedCount, ready,
      refresh, setActive, connect, disconnect, remove, test, add, autoDetect,
    }}>
      {children}
    </BrokerCtx.Provider>
  );
}

export function useBroker() {
  const v = useContext(BrokerCtx);
  if (!v) throw new Error("useBroker 必须在 BrokerProvider 内使用");
  return v;
}
