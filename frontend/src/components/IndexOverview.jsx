// E3 市场概览（指数分析页）：统计类板块真实家数（涨跌/停板/各市场广度）
//   + 主要指数快照 + 宽度趋势（涨跌家数日K）+ 两市成交额（上证+深证快照求和，C4）。
// 数据：/market/overview（真实 TDX 公共行情，零 mock；聚合不可得时显「—」）。
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import Chart from "./Chart.jsx";
import { formatPct, formatAmount } from "../hooks/useMarket.js";
import { navToQuote } from "../lib/nav.js";

const pctCls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");

export default function IndexOverview() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  // C1：数据源切换（auto/broker/eltdx）
  const [srcSel, setSrcSel] = useState("auto");

  useEffect(() => {
    setLoading(true); setErr("");
    api.marketOverview({ source: srcSel })
      .then((r) => setData(r))
      .catch((e) => setErr(e.message || "市场概览获取失败"))
      .finally(() => setLoading(false));
  }, [srcSel]);

  const trendOption = useMemo(() => {
    const s = data?.breadth_trend?.series || [];
    if (!s.length) return null;
    return {
      animation: false,
      grid: { left: 50, right: 14, top: 16, bottom: 24 },
      xAxis: { type: "category", data: s.map((x) => x.date), axisLabel: { color: "#5a6a82", fontSize: 10 }, axisLine: { lineStyle: { color: "#3a4a66" } } },
      yAxis: { scale: true, splitLine: { lineStyle: { color: "rgba(58,74,102,.3)" } }, axisLabel: { color: "#5a6a82", fontSize: 10 } },
      tooltip: { trigger: "axis" },
      series: [{
        name: "涨跌家数", type: "line", data: s.map((x) => x.value), showSymbol: false,
        lineStyle: { width: 1.5, color: "#4f8cff" }, areaStyle: { color: "rgba(79,140,255,.12)" },
      }],
    };
  }, [data]);

  return (
    <div className="page index-overview">
      <div className="io-head">
        <h3>市场概览 / 指数分析</h3>
        {/* C1：数据源切换（请求透传 source 参数） */}
        <select value={srcSel} onChange={(e) => setSrcSel(e.target.value)} title="行情数据源">
          <option value="auto">源:自动</option>
          <option value="broker">源:券商</option>
          <option value="eltdx">源:公共</option>
        </select>
        {data?.source && <span className="tag ok">数据源：{data.source}</span>}
        {loading && <span className="muted">加载中…</span>}
        {err && <span className="down">{err}</span>}
      </div>

      {data && (
        <>
          {/* 市场广度（统计类板块真实家数） */}
          <div className="io-section">
            <div className="io-section-title">市场广度（涨跌/停板家数 · 真实口径）</div>
            <div className="io-breadth">
              {(data.breadth || []).map((b) => (
                <div key={b.code} className="io-bcard card" title={`${b.name}（${b.code}）`}>
                  <div className="io-bname">{b.name}</div>
                  <div className="io-bval">{b.count != null ? b.count : "—"}</div>
                  <div className="io-bunit">家{typeof b.unit === "string" && b.unit && b.unit !== "家" ? `（${b.unit}）` : ""}</div>
                </div>
              ))}
              {(!data.breadth || !data.breadth.length) && <div className="muted">暂无广度数据</div>}
            </div>
          </div>

          {/* 主要指数快照 */}
          <div className="io-section">
            <div className="io-section-title">主要指数</div>
            <div className="io-indices">
              {(data.indices || []).map((x) => (
                <div key={x.code} className="io-icard card" onClick={() => navToQuote(x.code)}
                  title="点击查看指数行情">
                  <div className="io-iname">{x.name || x.code}</div>
                  <div className={`io-ilast ${pctCls(x.change_pct)}`}>
                    {x.last != null ? Number(x.last).toFixed(2) : "—"}</div>
                  <div className={`io-ipct ${pctCls(x.change_pct)}`}>
                    {x.change_pct != null ? formatPct(x.change_pct) : "—"}</div>
                  {x.amount != null && <div className="io-iamt muted">成交额 {formatAmount(x.amount)}</div>}
                </div>
              ))}
              {(!data.indices || !data.indices.length) && <div className="muted">暂无指数数据</div>}
            </div>
          </div>

          {/* 宽度趋势 + 两市成交额 */}
          <div className="io-section io-row2">
            <div className="card io-trend">
              <div className="io-section-title">宽度趋势（涨跌家数 · 近 30 日）</div>
              {trendOption
                ? <Chart option={trendOption} height={220} />
                : <div className="muted">暂无趋势数据</div>}
            </div>
            <div className="card io-turnover">
              <div className="io-section-title">两市成交额</div>
              <div className="io-tv-val">
                {data.two_city_turnover != null ? formatAmount(data.two_city_turnover) : "—"}
              </div>
              <div className="muted io-tv-note">
                {data.two_city_note || "指数快照缺成交额，无法聚合两市成交额"}
              </div>
            </div>
          </div>
        </>
      )}

      {!data && !loading && !err && <div className="muted io-empty">加载市场概览…</div>}
    </div>
  );
}
