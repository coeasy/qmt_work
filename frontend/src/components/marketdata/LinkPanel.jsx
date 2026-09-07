// 同板块联动抽屉面板（自 MarketData.jsx 原样拆出，行为零变更）。
import { navToQuote } from "../../lib/nav.js";
import { formatPct } from "../../hooks/useMarket.js";

export default function LinkPanel({ code, linkName, linkCons, linkLoading, linkQuotes,
  industryName, conceptList, loadLink }) {
  return (
    <div className="card link-card">
      <div className="link-head">
        <span>同板块联动 · <b>{linkName || "—"}</b></span>
        <span className="muted">{linkCons?.total != null ? `共 ${linkCons.total} 只成分` : ""}</span>
        {linkLoading && <span className="muted">加载中…</span>}
      </div>
      <div className="link-chips">
        {industryName && (
          <button className={`qp-concept-tag ${linkName === industryName ? "active" : ""}`}
            onClick={() => loadLink(industryName)}>{industryName}</button>
        )}
        {conceptList.slice(0, 8).map((c) => (
          <button key={c} className={`qp-concept-tag ${linkName === c ? "active" : ""}`}
            onClick={() => loadLink(c)}>{c}</button>
        ))}
      </div>
      <table className="bd-table link-tbl">
        <thead><tr><th>股票</th><th>代码</th><th className="num">最新</th><th className="num">涨跌幅</th></tr></thead>
        <tbody>
          {(linkCons?.items || []).map((c) => {
            const q = linkQuotes[c.code];
            const lnLast = q?.last != null ? Number(q.last) : c.last;
            const lnPct = q?.change_pct != null ? Number(q.change_pct) : c.change_pct;
            const isSelf = c.code === code;
            return (
              <tr key={c.code} className="bd-stock" onClick={() => navToQuote(c.code)}>
                <td>{isSelf ? `${c.name || "—"} ◀` : (c.name || "—")}</td>
                <td className="code">{c.code}</td>
                <td className="num">{lnLast != null ? Number(lnLast).toFixed(2) : "—"}</td>
                <td className={`num ${lnPct == null ? "" : lnPct >= 0 ? "up" : "down"}`}>
                  {lnPct != null ? formatPct(lnPct) : "—"}</td>
              </tr>
            );
          })}
          {!linkLoading && linkCons && !linkCons.items?.length &&
            <tr><td colSpan={4} className="muted">该板块暂无成分股数据</td></tr>}
          {linkLoading && <tr><td colSpan={4} className="muted">同板块成分股加载中…</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
