// 自选股：分组管理 + 实时行情表格 + 涨跌幅着色。
// v2 新增独立页面（原 BottomDock 自选股 Tab 提升）。
import { useState, useMemo } from "react";
import { useQuotes } from "../../lib/quoteHub.jsx";
import { navToQuote } from "../../lib/nav.js";
import usePersistentState from "../../lib/usePersistentState.js";

const DEFAULT_WATCHLIST = ["600519", "000001", "300750", "000858", "601318"];
const WATCH_KEY = "qmt_work.watchlist.v2";

export default function Watchlist() {
  const [codes, setCodes] = usePersistentState(WATCH_KEY, DEFAULT_WATCHLIST);
  const [addCode, setAddCode] = useState("");
  const { quotes } = useQuotes(codes);

  const handleAdd = () => {
    const c = addCode.trim().replace(/\D/g, "");
    if (c.length === 6 && !codes.includes(c)) {
      setCodes((prev) => [...prev, c]);
    }
    setAddCode("");
  };

  const handleRemove = (code) => {
    setCodes((prev) => prev.filter((x) => x !== code));
  };

  const rows = useMemo(() => codes.map((code) => ({ code, ...(quotes[code] || {}) })).filter(r => r.code), [codes, quotes]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "8px 12px", display: "flex", gap: 8, alignItems: "center", borderBottom: "1px solid var(--border)" }}>
        <input
          type="text"
          placeholder="添加自选代码"
          value={addCode}
          onChange={(e) => setAddCode(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
          style={{ width: 140, padding: "4px 8px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 4, color: "var(--text)" }}
        />
        <button className="btn btn-sm" onClick={handleAdd}>添加</button>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>共 {codes.length} 只</span>
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        <table className="data-table" style={{ width: "100%" }}>
          <thead>
            <tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>涨跌额</th><th>成交量</th><th>成交额</th><th>换手率</th><th>操作</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const pct = r.pct || 0;
              const bg = pct > 0 ? "var(--up-soft)" : pct < 0 ? "var(--down-soft)" : "";
              return (
                <tr key={r.code} style={{ background: bg, cursor: "pointer" }} onClick={() => navToQuote(r.code)}>
                  <td>{r.code}</td>
                  <td>{r.name || "-"}</td>
                  <td style={{ color: pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--text)" }}>{r.last?.toFixed(2) || "-"}</td>
                  <td style={{ color: pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--text)", fontWeight: 600 }}>{pct ? `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%` : "-"}</td>
                  <td style={{ color: (r.change || 0) > 0 ? "var(--up)" : (r.change || 0) < 0 ? "var(--down)" : "var(--text)" }}>{r.change ? r.change.toFixed(2) : "-"}</td>
                  <td>{r.volume ? (r.volume / 1e4).toFixed(1) + "万" : "-"}</td>
                  <td>{r.amount ? (r.amount / 1e8).toFixed(2) + "亿" : "-"}</td>
                  <td>{r.turnover ? r.turnover.toFixed(2) + "%" : "-"}</td>
                  <td onClick={(e) => { e.stopPropagation(); handleRemove(r.code); }}><span style={{ color: "var(--danger)", cursor: "pointer" }}>×</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
