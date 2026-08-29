// ETF 基金（东财对标）：全市场 ETF 清单 + 实时染色 + 搜索/排序/分组/涨跌幅区间筛选。
// 数据：/market/etfs（代码段 51/56/58/15/16，limit=0 返回全量，避免整段截断 P0-2）。
// 实时：首屏只拉清单（快，后端 5min TTL 缓存）；可见页行经 QuoteHub 引用计数订阅，
//       零轮询实时刷新；离开页面/翻页自动退订（P0-4 延迟 60s 宽限退订）。
// H2 ETF 详情：点击行打开抽屉，展示实时价 + K 线 + 近 1 月波动率（真实口径）。
//       跟踪指数 / 规模 / 持仓 eltdx 公共源未提供，明确标注不造数。
// H3 筛选：分组 + 关键词 + 排序 + 涨跌幅区间（多维筛选）。
// 约束：IOPV / 规模等取不到的字段不显示（不造数）；点击「行情分析」深链个股分析页。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { useQuotes } from "../lib/quoteHub.jsx";
import Chart from "./Chart.jsx";
import { formatPct, formatAmount } from "../hooks/useMarket.js";
import { navTo, navToQuote } from "../lib/nav.js";

const PAGE_SIZE = 50;
const SORTS = [
  { v: "code", label: "按代码" },
  { v: "pct", label: "按涨跌幅" },
  { v: "amount", label: "按成交额" },
];
const ETF_GROUPS = [
  { v: "", label: "全部" },
  { v: "51", label: "沪 ETF" },
  { v: "56", label: "沪行业/主题" },
  { v: "58", label: "沪跨境/商品" },
  { v: "15", label: "深 ETF" },
  { v: "16", label: "深 LOF/分级" },
];

const pctCls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");

export default function Etfs() {
  const [rows, setRows] = useState([]);
  const [source, setSource] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [kw, setKw] = useState("");
  const [sortBy, setSortBy] = useState("code");
  const [group, setGroup] = useState("");
  const [page, setPage] = useState(0);
  // H3 涨跌幅区间筛选
  const [pctMin, setPctMin] = useState("");
  const [pctMax, setPctMax] = useState("");
  // H2 详情抽屉
  const [sel, setSel] = useState(null);   // {code, name}
  // C1：数据源选择（auto/broker/eltdx），ref 供 load 闭包读取最新值
  const [srcSel, setSrcSel] = useState("auto");
  const srcRef = useRef(srcSel);
  srcRef.current = srcSel;

  const load = () => {
    setLoading(true); setErr("");
    // P0-1：首屏只拉清单（limit=0 全量，后端 5min TTL 缓存），不请求全量快照。
    api.marketEtfs({ limit: 0, with_quote: false, source: srcRef.current })
      .then((r) => {
        setRows(r.items || []); setSource(r.source || "");
        setPage(0);
      })
      .catch((e) => { setRows([]); setErr(e.message || "ETF 清单获取失败"); })
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, [srcSel]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ---------- 多维过滤 + 排序 + 分页 ---------- */
  const filtered = useMemo(() => {
    const k = kw.trim().toLowerCase();
    const lo = pctMin === "" ? null : Number(pctMin);
    const hi = pctMax === "" ? null : Number(pctMax);
    let list = rows;
    if (group) list = list.filter((r) => r.code.startsWith(group));
    if (k) list = list.filter((r) => r.code.toLowerCase().includes(k) || (r.name || "").toLowerCase().includes(k));
    if (lo != null || hi != null) {
      list = list.filter((r) => {
        const p = r.change_pct != null ? r.change_pct : null;
        if (p == null) return false;
        if (lo != null && p < lo) return false;
        if (hi != null && p > hi) return false;
        return true;
      });
    }
    const key = (r) => {
      if (sortBy === "pct") return r.change_pct != null ? r.change_pct : -999;
      if (sortBy === "amount") return r.amount != null ? r.amount : -1;
      return r.code;
    };
    return [...list].sort((a, b) => {
      const ka = key(a), kb = key(b);
      if (sortBy === "code") return String(ka).localeCompare(String(kb));
      return kb - ka;
    });
  }, [rows, kw, sortBy, group, pctMin, pctMax]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = useMemo(
    () => filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [filtered, page],
  );
  useEffect(() => { if (page >= pageCount) setPage(0); }, [page, pageCount]);

  /* ---------- 可见行实时订阅（QuoteHub 引用计数，翻页/离开自动退订） ---------- */
  const liveCodes = useMemo(() => pageRows.map((r) => r.code), [pageRows]);
  const { quotes } = useQuotes(liveCodes);

  /* ---------- H2 详情：选中 ETF 的实时价 + K 线 + 近 1 月波动 ---------- */
  const { quotes: selQuotes } = useQuotes(sel ? [sel.code] : []);
  const [kline, setKline] = useState([]);
  const [kErr, setKErr] = useState("");
  useEffect(() => {
    if (!sel) { setKline([]); return; }
    setKErr("");
    api.marketKline({ code: sel.code, period: "1d", count: 21 })
      .then((r) => setKline((r && (r.bars || r.data?.bars)) || []))
      .catch(() => setKErr("K 线获取失败"));
  }, [sel]);

  // 近 1 月波动率：日收益率标准差（真实日 K 计算，不估算）
  const vol1m = useMemo(() => {
    const closes = kline.map((b) => Number(b.close)).filter((v) => v);
    if (closes.length < 3) return null;
    const rets = [];
    for (let i = 1; i < closes.length; i++) rets.push(closes[i] / closes[i - 1] - 1);
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const variance = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length;
    return Math.sqrt(variance) * 100; // 日波动率 %
  }, [kline]);

  const klineOption = useMemo(() => {
    if (!kline.length) return null;
    const dates = kline.map((b) => b.time);
    const closes = kline.map((b) => Number(b.close));
    return {
      animation: false,
      grid: { left: 52, right: 12, top: 12, bottom: 22 },
      xAxis: { type: "category", data: dates, axisLabel: { color: "#5a6a82", fontSize: 10 }, axisLine: { lineStyle: { color: "#3a4a66" } } },
      yAxis: { scale: true, splitLine: { lineStyle: { color: "rgba(58,74,102,.3)" } }, axisLabel: { color: "#5a6a82", fontSize: 10 } },
      tooltip: { trigger: "axis" },
      series: [{
        name: "ETF", type: "line", data: closes, showSymbol: false,
        lineStyle: { width: 1.5, color: closes[closes.length - 1] >= closes[0] ? "#ef4d56" : "#29c08a" },
        areaStyle: { color: "rgba(79,140,255,.10)" },
      }],
    };
  }, [kline]);

  const selQ = sel ? selQuotes[sel.code] : null;
  const selLast = selQ?.last != null ? Number(selQ.last) : null;
  const selPct = selQ?.change_pct != null ? Number(selQ.change_pct) : null;

  return (
    <div className="page etfs-page">
      {/* 工具栏：分组 + 搜索 + 涨跌幅区间 + 排序 + 刷新 */}
      <div className="etf-toolbar">
        <div className="bd-tabs">
          {ETF_GROUPS.map((g) => (
            <button key={g.v} className={`bd-tab ${group === g.v ? "active" : ""}`}
              onClick={() => { setGroup(g.v); setPage(0); }}>{g.label}</button>
          ))}
        </div>
        <input className="etf-search" value={kw} placeholder="搜索代码 / 名称"
          onChange={(e) => { setKw(e.target.value); setPage(0); }} />
        <span className="etf-range" title="涨跌幅区间筛选">
          <input className="num" type="number" value={pctMin} placeholder="涨%≥"
            onChange={(e) => { setPctMin(e.target.value); setPage(0); }} />
          <span>~</span>
          <input className="num" type="number" value={pctMax} placeholder="≤"
            onChange={(e) => { setPctMax(e.target.value); setPage(0); }} />
        </span>
        <select value={sortBy} onChange={(e) => { setSortBy(e.target.value); setPage(0); }}>
          {SORTS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        {/* C1：数据源切换（请求透传 source 参数） */}
        <select value={srcSel} onChange={(e) => setSrcSel(e.target.value)} title="行情数据源">
          <option value="auto">源:自动</option>
          <option value="broker">源:券商</option>
          <option value="eltdx">源:公共</option>
        </select>
        <button className="ghost btn-sm" onClick={load} disabled={loading}>
          {loading ? "加载…" : "刷新"}
        </button>
        {source && <span className="tag ok">数据源：{source}</span>}
        <span className="muted">{filtered.length} 只</span>
        {err && <span className="down">{err}</span>}
      </div>

      <div className="etf-body">
        <div className="card etf-table-wrap">
          <table className="bd-table">
            <thead>
              <tr>
                <th>名称</th><th>代码</th><th className="num">最新</th>
                <th className="num">涨跌幅</th><th className="num">成交额</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((r) => {
                const q = quotes[r.code];
                const last = q?.last != null ? Number(q.last)
                  : (r.last != null ? Number(r.last) : null);
                const pct = q?.change_pct != null ? Number(q.change_pct) : r.change_pct;
                const amount = q?.amount != null ? Number(q.amount) : r.amount;
                return (
                  <tr key={r.code} className={`bd-stock ${sel?.code === r.code ? "sel" : ""}`}
                    title="点击查看 ETF 详情"
                    onClick={() => setSel({ code: r.code, name: r.name })}>
                    <td>{r.name || "—"}</td>
                    <td className="code">{r.code}</td>
                    <td className={`num ${pctCls(pct)}`}>{last != null ? last.toFixed(3) : "—"}</td>
                    <td className={`num ${pctCls(pct)}`}>{pct != null ? formatPct(pct) : "—"}</td>
                    <td className="num">{amount != null ? formatAmount(amount) : "—"}</td>
                  </tr>
                );
              })}
              {!pageRows.length && !loading && (
                <tr><td colSpan={5} className="muted">{err || "暂无 ETF 数据（TDX 行情源未连接）"}</td></tr>
              )}
            </tbody>
          </table>
        </div>

        {/* H2 ETF 详情抽屉 */}
        {sel && (
          <div className="card etf-detail">
            <div className="etf-detail-head">
              <span className="etf-d-name">{sel.name}</span>
              <span className="code">{sel.code}</span>
              {selLast != null && <span className={`etf-d-last ${pctCls(selPct)}`}>{selLast.toFixed(3)}</span>}
              {selPct != null && <span className={`etf-d-pct ${pctCls(selPct)}`}>{formatPct(selPct)}</span>}
              <button className="ghost btn-sm" onClick={() => setSel(null)}>关闭</button>
              <button className="ghost btn-sm" onClick={() => navToQuote(sel.code)}>行情分析打开</button>
              {/* C3：一步到交易页预填（code + 现价限价单），沿用 navTo 参数模式 */}
              <button className="ghost btn-sm" title="跳转手动交易并预填代码/限价"
                onClick={() => navTo("trade", { params: {
                  code: sel.code, direction: "buy",
                  ...(selLast != null ? { price: selLast } : {}),
                } })}>买入</button>
            </div>
            <div className="etf-d-metrics">
              <div className="etf-m"><span>近 1 月波动率</span>
                <b>{vol1m != null ? vol1m.toFixed(2) + "%" : "—"}</b></div>
              <div className="etf-m"><span>跟踪指数 / 规模 / 持仓</span>
                <b className="muted">未提供</b></div>
            </div>
            <div className="etf-d-chart">
              {kErr && <div className="down">{kErr}</div>}
              {!kErr && klineOption
                ? <Chart option={klineOption} height={200} />
                : !kErr && <div className="muted">K 线加载中…</div>}
            </div>
            <div className="muted etf-d-note">
              跟踪指数 / 规模 / 持仓为基金公司披露字段，公共行情源（TDX）不提供，故标注「未提供」不造数；
              接券商或补充 F10 源后可扩展。
            </div>
          </div>
        )}
      </div>

      {/* 分页 */}
      {pageCount > 1 && (
        <div className="etf-pager">
          <button className="ghost btn-sm" disabled={page === 0}
            onClick={() => setPage((p) => p - 1)}>上一页</button>
          <span className="muted">{page + 1} / {pageCount}</span>
          <button className="ghost btn-sm" disabled={page >= pageCount - 1}
            onClick={() => setPage((p) => p + 1)}>下一页</button>
        </div>
      )}
    </div>
  );
}
