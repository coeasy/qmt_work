// 多维摘要数据 hook（I1，东财对标）：资金流 + 流通股本/涨跌停。
// 自 MarketData.jsx 原样拆出：真实口径（/market/moneyflow est=false，/market/capital）；
// code 变更拉一次 + 手动刷新，不轮询（J2）；任一字段缺失显示「—」，绝不估算填充。
// G4 数据面：资金流/股本走 topic 总线（切回已看代码秒出快照，同页不重复请求）。
import { useEffect, useState, useCallback } from "react";
import { api } from "../api.js";
import { subscribe as hubSubscribe, invalidate } from "../lib/dataHub.js";

export function useMfSummary(code) {
  const [mf, setMf] = useState(null);          // /market/moneyflow：net/inside/outside/strength/volume_ratio
  const [mfErr, setMfErr] = useState("");
  const [cap, setCap] = useState(null);        // /market/capital：shares + limits
  const [mfRefresh, setMfRefresh] = useState(0);
  useEffect(() => {
    if (!code) return;
    setMf(null); setMfErr(""); setCap(null);
    const unsub1 = hubSubscribe(`market:moneyflow:${code}`, async () => {
      const r = await api.marketMoneyflow({ code });
      return r;
    }, ({ data, error }) => {
      if (data) { setMf(data); setMfErr(""); }
      else if (error) setMfErr(error.message || "资金流暂不可用");
    });
    const unsub2 = hubSubscribe(`market:capital:${code}`, async () => {
      const r = await api.marketCapital({ codes: code });
      return r;
    }, ({ data }) => { if (data) setCap(data); });   // 股本/涨跌停缺失 → 摘要卡显「—」
    return () => { unsub1(); unsub2(); };
  }, [code, mfRefresh]);
  const refreshMf = useCallback(() => {
    if (code) { invalidate(`market:moneyflow:${code}`); invalidate(`market:capital:${code}`); }
    setMfRefresh(Date.now());
  }, [code]);
  return { mf, mfErr, cap, refreshMf, mfRefresh };
}
