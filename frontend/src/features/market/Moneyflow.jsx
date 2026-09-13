// 资金流：自选股主力内外盘真实口径 + 量比。
// v2 新增独立页面（原 BottomDock 资金流 Tab 提升）。
// 后端 /market/moneyflow 是「单只标的」接口，因此本页按自选池并发查询，
// 不伪造全市场排行榜，也不误把单票接口当榜单接口调用。
import { useEffect, useState, useMemo, useCallback } from "react";
import { api } from "../../api.js";
import { useQuotes } from "../../lib/quoteHub.jsx";
import { navToQuote } from "../../lib/nav.js";
import usePersistentState from "../../lib/usePersistentState.js";

const DEFAULT_WATCHLIST = ["600519", "000001", "300750", "000858", "601318"];
const WATCH_KEY = "qmt_work.watchlist.v2";
const MAX_MONEYFLOW_CODES = 20;

function fmtLots(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(2)}万手`;
  return `${n.toFixed(2)}手`;
}

export default function Moneyflow() {
  const [codes, setCodes] = usePersistentState(WATCH_KEY, DEFAULT_WATCHLIST);
  const [addCode, setAddCode] = useState("");
  const [rows, setRows] = useState([]);
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(false);
  const { quotes } = useQuotes(codes);

  const activeCodes = useMemo(() => codes.slice(0, MAX_MONEYFLOW_CODES), [codes]);

  const load = useCallback(() => {
    if (activeCodes.length === 0) {
      setRows([]);
      setErrors([]);
      return;
    }
    setLoading(true);
    const task = activeCodes.map(async (code) => {
      try {
        const d = await api.marketMoneyflow({ code });
        return { code, data: d, error: "" };
      } catch (e) {
        return { code, data: null, error: e?.message || "获取失败" };
      }
    });
    Promise.allSettled(task).then((results) => {
      const okRows = results.filter((x) => x.status === "fulfilled" && x.value?.data)
        .map((x) => ({ code: x.value.code, ...(x.value.data || {}) }));
      setRows(okRows);
      setErrors(results.filter((x) => x.status === "fulfilled" && x.value?.error)
        .map((x) => ({ code: x.value.code, error: x.value.error })));
      setLoading(false);
    });
  }, [activeCodes]);

  useEffect(() => { load(); }, [load]);

  const handleAdd = () => {
    const c = addCode.trim().replace(/\D/g, "");
    if (c.length === 6 && !codes.includes(c)) setCodes((prev) => [...prev, c]);
    setAddCode("");
  };

  // 真实口径：inside/outside 为外盘-内盘（单位：手），后端缺失即显示「—」。
  const sorted = useMemo(() =>
    [...rows].sort((a, b) => Math.abs(b.net || 0) - Math.abs(a.net || 0)),
  [rows]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "8px 12px", display: "flex", gap: 8, alignItems: "center", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
        <input
          type="text"
          placeholder="添加股票代码"
          value={addCode}
          onChange={(e) => setAddCode(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
          style={{ width: 140, padding: "4px 8px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 4, color: "var(--text)" }}
        />
        <button className="btn btn-sm" onClick={handleAdd}>添加</button>
        <button className="btn btn-sm ghost" onClick={load} disabled={loading}>刷新</button>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
          {loading ? "加载中…" : `自选池 ${rows.length}/${activeCodes.length} 只可取 · 最多查询 ${MAX_MONEYFLOW_CODES} 只`}
        </span>
        <span style={{ fontSize: 11, color: "var(--text-dim)" }}>口径：TDX 外盘-内盘，单位手</span>
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {activeCodes.length === 0 && (
          <div style={{ padding: 24, color: "var(--text-dim)", textAlign: "center" }}>请先添加自选代码后查看资金流</div>
        )}
        {rows.length === 0 && !loading && activeCodes.length > 0 && (
          <div style={{ padding: 24, color: "var(--danger)", textAlign: "center" }}>
            {errors.length > 0 ? `${errors[0].code}：${errors[0].error}` : "当前自选池暂无可用资金流数据"}
          </div>
        )}
        <table className="data-table" style={{ width: "100%" }}>
          <thead>
            <tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>外盘</th><th>内盘</th><th>净额</th><th>量比</th></tr>
          </thead>
          <tbody>
            {sorted.map((d) => {
              const q = quotes[d.code] || {};
              const pct = q.change_pct ?? q.pct ?? 0;
              const net = d.net;
              return (
                <tr key={d.code} style={{ cursor: "pointer", background: (net || 0) > 0 ? "var(--up-soft)" : (net || 0) < 0 ? "var(--down-soft)" : "" }}
                  onClick={() => navToQuote(d.code)}>
                  <td>{d.code}</td>
                  <td>{q.name || d.name || "—"}</td>
                  <td>{q.last != null ? Number(q.last).toFixed(2) : "—"}</td>
                  <td style={{ color: pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--text)" }}>
                    {q.change_pct == null && q.pct == null
                      ? "—"
                      : `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`}
                  </td>
                  <td>{fmtLots(d.outside)}</td>
                  <td>{fmtLots(d.inside)}</td>
                  <td style={{ color: net > 0 ? "var(--up)" : net < 0 ? "var(--down)" : "var(--text)", fontWeight: 700 }}>
                    {net == null ? "—" : `${net > 0 ? "+" : "-"}${fmtLots(Math.abs(net))}`}
                  </td>
                  <td>{d.volume_ratio != null ? Number(d.volume_ratio).toFixed(2) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
