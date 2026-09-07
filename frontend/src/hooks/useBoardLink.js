// I2 同板块联动 hook：当前票的所属行业/概念板块内其他成分股（实时）。
// 自 MarketData.jsx 原样拆出：经 /market/board/lookup 把行业/概念名称解析为确切板块代码，
// 再取成分股（F3 稳化链路复用）；组件层用 useQuotes(linkCodes) 订阅实时价。
import { useCallback, useMemo, useState } from "react";
import { api } from "../api.js";
import { useQuotes } from "../lib/quoteHub.jsx";

export function useBoardLink(code, industryName) {
  const [linkName, setLinkName] = useState("");
  const [linkCode, setLinkCode] = useState(null);
  const [linkCons, setLinkCons] = useState(null);
  const [linkLoading, setLinkLoading] = useState(false);

  // 订阅板块成分股（前 200 只）实时价，离开/切换自动退订（延迟 60s 宽限）。
  const linkCodes = useMemo(
    () => (linkCons?.items || []).slice(0, 200).map((c) => c.code), [linkCons]);
  const { quotes: linkQuotes } = useQuotes(linkCodes);

  const loadLink = useCallback(async (nm) => {
    if (!nm) return;
    setLinkName(nm); setLinkLoading(true); setLinkCons(null);
    try {
      const r = await api.marketBoardLookup({ name: nm, limit: 1 });
      const m = (r && r.matches && r.matches[0]) || null;
      if (!m || !m.code) { setLinkLoading(false); return; }
      setLinkCode(m.code);
      const first = await api.marketBoardConstituents({ code: m.code, limit: 200, page: 0 });
      setLinkCons(first || { items: [] });
    } catch {
      setLinkCons({ items: [] });
    } finally {
      setLinkLoading(false);
    }
  }, []);

  // 切换股票/无行业时清空联动状态（原组件 else 分支的等价物）
  const resetLink = useCallback(() => {
    setLinkCode(null); setLinkCons(null); setLinkName("");
  }, []);

  return { linkName, linkCode, linkCons, linkLoading, linkQuotes, linkCodes, loadLink, resetLink };
}
