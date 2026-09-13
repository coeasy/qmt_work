// 板块雷达：行业/概念板块涨跌排行 + 成分股联动。
// v2 新增独立页面（原 BottomDock 板块 Tab 提升）。
import { useEffect, useState, useMemo } from "react";
import { api } from "../../api.js";
import { useQuotes } from "../../lib/quoteHub.jsx";
import { navToQuote } from "../../lib/nav.js";
import { fmtAmount } from "../../lib/format.js";

export default function SectorRadar() {
  const [view, setView] = useState("industry");
  const [boards, setBoards] = useState([]);
  const [selected, setSelected] = useState(null);
  const [constituents, setConstituents] = useState([]);
  const [codes, setCodes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const { quotes } = useQuotes(codes);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    api.marketBoards({ kind: view, limit: 60 })
      .then((d) => {
        if (!alive) return;
        const items = d?.items || [];
        setBoards(items);
        setCodes(items.slice(0, 12).map((b) => b.code).filter(Boolean));
        setLoading(false);
      })
      .catch((e) => {
        if (!alive) return;
        setBoards([]);
        setCodes([]);
        setErr(e?.message || "板块榜获取失败");
        setLoading(false);
      });
    return () => { alive = false; };
  }, [view]);

  useEffect(() => {
    if (!selected?.code) { setConstituents([]); return; }
    let alive = true;
    api.marketBoardConstituents({ code: selected.code, limit: 80 })
      .then((d) => {
        if (alive) setConstituents(d?.items || []);
      })
      .catch(() => {
        if (alive) setConstituents([]);
      });
    return () => { alive = false; };
  }, [selected?.code]);

  // 后端真实字段是 change_pct（不是旧前端误用的 pct）。
  const sorted = useMemo(() =>
    [...boards].sort((a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0)),
  [boards]);

  return (
    <div style={{ display: "flex", height: "100%", gap: 0 }}>
      <div style={{ flex: 1, overflow: "auto", borderRight: "1px solid var(--border)" }}>
        <div style={{ padding: "8px 12px", display: "flex", gap: 8, borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
          <button className="btn btn-sm" onClick={() => setView("industry")} style={view === "industry" ? { background: "var(--accent)" } : {}}>行业板块</button>
          <button className="btn btn-sm" onClick={() => setView("concept")} style={view === "concept" ? { background: "var(--accent)" } : {}}>概念板块</button>
          {loading && <span style={{ fontSize: 12, color: "var(--text-dim)" }}>加载中…</span>}
          {err && <span style={{ fontSize: 12, color: "var(--danger)" }}>{err}</span>}
        </div>
        {sorted.length === 0 && !loading && !err && (
          <div style={{ padding: 24, color: "var(--text-dim)", textAlign: "center" }}>暂无板块行情数据</div>
        )}
        <table className="data-table" style={{ width: "100%" }}>
          <thead>
            <tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>成交额</th><th>量</th></tr>
          </thead>
          <tbody>
            {sorted.map((b) => {
              const pct = b.change_pct;
              return (
                <tr key={b.code} style={{ cursor: "pointer", background: pct > 0 ? "var(--up-soft)" : pct < 0 ? "var(--down-soft)" : "" }}
                  onClick={() => setSelected(b)}>
                  <td>{b.code}</td>
                  <td>{b.name}</td>
                  <td>{b.last != null ? Number(b.last).toFixed(2) : "—"}</td>
                  <td style={{ color: pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--text)", fontWeight: 600 }}>
                    {pct != null ? `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%` : "—"}
                  </td>
                  <td>{b.amount != null ? fmtAmount(b.amount) : "—"}</td>
                  <td>{b.volume != null ? fmtAmount(b.volume) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ width: 320, overflow: "auto", padding: "8px 12px" }}>
        <div style={{ marginBottom: 8, fontWeight: 600, color: "var(--accent)" }}>
          {selected ? `${selected.name} 成分股` : "点击左侧板块查看成分股"}
        </div>
        {selected && constituents.length === 0 && (
          <div style={{ color: "var(--text-dim)", fontSize: 12 }}>暂无成分股数据</div>
        )}
        {selected && constituents.map((c) => (
          <div key={c.code} style={{ padding: "4px 8px", cursor: "pointer", display: "flex", justifyContent: "space-between" }}
            onClick={() => navToQuote(c.code)}>
            <span>{c.code} {c.name}</span>
            {(() => {
              const q = quotes[c.code];
              if (!q || q.last == null) return null;
              const pct = q.change_pct ?? q.pct ?? 0;
              return <span style={{ color: pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--text-dim)" }}>
                {Number(q.last).toFixed(2)} {pct > 0 ? "+" : ""}{pct.toFixed(2)}%
              </span>;
            })()}
          </div>
        ))}
      </div>
    </div>
  );
}
