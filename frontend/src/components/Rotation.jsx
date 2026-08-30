// F4 板块轮动视图：topN 板块近 N 日每日涨跌幅热力图（真实板块指数日K 计算）。
// 数据：/market/rotation（真实 TDX 板块指数，零 mock）。
import { useEffect, useState } from "react";
import { api } from "../api.js";
import { subscribe, invalidate } from "../lib/dataHub.js";
import { formatPct } from "../hooks/useMarket.js";

// 涨跌幅 → 背景色（红涨绿跌，CN 惯例；强度随绝对值 0~5% 线性，封顶饱和）。
function heatBg(pct) {
  if (pct == null) return "transparent";
  const m = Math.min(Math.abs(pct), 5) / 5;       // 0..1
  const a = (0.12 + m * 0.55).toFixed(2);
  return pct >= 0 ? `rgba(239,77,86,${a})` : `rgba(41,192,138,${a})`;
}

export default function Rotation() {
  const [days, setDays] = useState(5);
  const [kind, setKind] = useState("industry");
  const [topN, setTopN] = useState(40);
  const [rows, setRows] = useState([]);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  // G4 数据面：market:rotation 策略驱动缓存/节流；手动刷新 = invalidate + 重订阅
  useEffect(() => {
    setLoading(true); setErr("");
    const unsub = subscribe(`market:rotation:${days}:${kind}:${topN}`, async () => {
      const r = await api.marketRotation({ days, kind, top_n: topN });
      return r;
    }, ({ data, error }) => {
      if (data) { setRows(data.boards || []); setMeta(data); }
      else if (error) { setRows([]); setErr(error.message || "板块轮动获取失败"); }
      setLoading(false);
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days, kind, topN, refreshKey]);
  const load = () => {
    invalidate(`market:rotation:${days}:${kind}:${topN}`);
    setRefreshKey(Date.now());
  };

  const dayCount = meta?.days || days;

  return (
    <div className="page rotation">
      <div className="rot-toolbar">
        <span className="rot-title">板块轮动</span>
        <label>回看
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[3, 5, 8, 10, 15, 20].map((d) => <option key={d} value={d}>{d} 日</option>)}
          </select>
        </label>
        <label>类型
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="industry">行业</option>
            <option value="concept">概念</option>
          </select>
        </label>
        <label>板块数
          <select value={topN} onChange={(e) => setTopN(Number(e.target.value))}>
            {[20, 30, 40, 50, 60].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <button className="ghost btn-sm" onClick={load} disabled={loading}>{loading ? "加载…" : "刷新"}</button>
        {meta?.source && <span className="tag ok">数据源：{meta.source}</span>}
        {err && <span className="down">{err}</span>}
      </div>

      <div className="rot-legend muted">
        色阶：<span className="rot-lg up">红 涨</span> / <span className="rot-lg down">绿 跌</span>
        （强度随涨跌幅绝对值；每行右侧为区间累计涨跌幅）
      </div>

      <div className="rot-grid-wrap card">
        {rows.length === 0 && !loading && <div className="muted rot-empty">暂无轮动数据</div>}
        {rows.length > 0 && (
          <table className="rot-table">
            <thead>
              <tr>
                <th className="rot-name">板块</th>
                {Array.from({ length: dayCount }).map((_, i) => (
                  <th key={i} className="num">T-{dayCount - i}</th>
                ))}
                <th className="num rot-cum">累计</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.code}>
                  <td className="rot-name" title={b.code}>{b.name}</td>
                  {Array.from({ length: dayCount }).map((_, i) => {
                    const d = b.daily[i];
                    return (
                      <td key={i} className="num rot-cell"
                        style={{ background: heatBg(d?.pct) }}>
                        {d?.pct != null ? formatPct(d.pct) : "—"}
                      </td>
                    );
                  })}
                  <td className={`num rot-cum ${b.cum_pct >= 0 ? "up" : "down"}`}>
                    {b.cum_pct != null ? formatPct(b.cum_pct) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
