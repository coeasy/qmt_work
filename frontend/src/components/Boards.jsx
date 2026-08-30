// 板块行情（东财 hsbk 对标）：行业 / 概念 / 统计类板块指数榜单 + 板块详情。
// 数据：/market/boards 真实板块指数快照（881xxx 行业 / 880xxx 概念，TDX 板块指数），
//       /market/board/constituents 成分股（f10 实时涨跌幅），/market/board/kline 板块 K 线。
// 约束：零轮询（手动刷新 + 切换拉取）；零 mock —— 任何字段取不到显示「—」；
//       成分股点击深链个股分析（navToQuote）；支持 params.code / params.name 深链定位板块
//       （个股页概念点击 → 本页联动，阶段 F3）。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import Chart from "./Chart.jsx";
import { PALETTE } from "../lib/chartPalette.js";
import { useChartSpec, onIndicatorChange } from "../lib/chartConfig.jsx";
import { formatPct, formatAmount } from "../hooks/useMarket.js";
import { navToQuote } from "../lib/nav.js";
import { useQuotes } from "../lib/quoteHub.jsx";
import { subscribe } from "../lib/dataHub.js";

const KIND_TABS = [
  { v: "industry", label: "行业板块" },
  { v: "concept", label: "概念板块" },
  { v: "stat", label: "统计类" },
];
const SORTS = [
  { v: "pct", label: "按涨跌幅" },
  { v: "amount", label: "按成交额" },
];
const BOARD_COUNT = 120;   // 榜单条数
const KLINE_COUNT = 60;    // 详情 K 线根数
// C1：数据源切换选项（与 /market/sources 的 auto 链路对齐）
const SRC_OPTS = [
  { v: "auto", label: "源:自动" },
  { v: "broker", label: "源:券商" },
  { v: "eltdx", label: "源:公共" },
];

const pctCls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");

export default function Boards({ params } = {}) {
  const [kind, setKind] = useState("industry");
  const [sortBy, setSortBy] = useState("pct");
  const { spec: chartSpec } = useChartSpec();   // G9 chart-spec 单一真源
  const [linkedInd, setLinkedInd] = useState(null); // G9 跨页联动指标（来自行情页）
  useEffect(() => onIndicatorChange((d) => setLinkedInd(d.sub || d.main || null)), []);
  const [rows, setRows] = useState([]);
  const [source, setSource] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const [selCode, setSelCode] = useState(null);   // 选中板块代码
  const [selName, setSelName] = useState("");
  const [cons, setCons] = useState(null);         // 成分股 {total, items}
  const [consLoading, setConsLoading] = useState(false);
  const [bars, setBars] = useState([]);           // 板块日 K
  const [detailErr, setDetailErr] = useState("");
  const [mf, setMf] = useState(null);             // G2 板块资金流聚合
  const [mfLoading, setMfLoading] = useState(false);
  // C1：数据源选择（auto/broker/eltdx）；ref 供深链 interval 闭包读取最新值
  const [srcSel, setSrcSel] = useState("auto");
  const srcRef = useRef(srcSel);
  srcRef.current = srcSel;

  // 成分股实时：订阅可见（前 200 只，避免超大板块订阅爆炸）经 QuoteHub 引用计数，
  // 离开/切换板块自动退订（P0-4 延迟 60s 宽限）。其余成分股保留 f10 拉取时的涨跌幅。
  const consCodes = useMemo(
    () => (cons?.items || []).slice(0, 200).map((c) => c.code), [cons]);
  const { quotes: consQuotes } = useQuotes(consCodes);

  /* ---------- 板块榜单（G4 数据面试点：topic 总线 + 后端策略表节流/合并） ---------- */
  const loadBoards = (k = kind, s = sortBy) => {
    setLoading(true); setErr("");
    api.marketBoards({ kind: k, sort_by: s, limit: BOARD_COUNT, source: srcRef.current })
      .then((r) => { setRows(r.items || []); setSource(r.source || ""); })
      .catch((e) => { setRows([]); setErr(e.message || "板块榜获取失败"); })
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    const unsub = subscribe(`market:boards:${kind}:${sortBy}`, async () => {
      const r = await api.marketBoards({ kind, sort_by: sortBy, limit: BOARD_COUNT,
                                         source: srcRef.current });
      setSource(r.source || "");
      return r.items || [];
    }, ({ data, error }) => {
      if (data) setRows(data);
      else if (error) { setRows([]); setErr(error.message || "板块榜获取失败"); }
      setLoading(false);
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, sortBy, srcSel]);

  /* ---------- 深链定位（F3：个股页概念/板块点击 → 本页） ---------- */
  // A2（R2）重构：不再在 setRows 的 updater 里调 selectBoard（副作用进 setState
  // 会让 StrictMode 下重复请求）。改为用 ref 保存最新 rows，interval 里读 ref 比对，
  // 命中后直接调 selectBoard —— setRows 仅用于渲染，二者彻底解耦。
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  useEffect(() => {
    const p = params || {};
    const want = (p.code || "").trim();
    const wantName = (p.name || "").trim();
    if (!want && !wantName) return;
    if (want.slice(2, 5) === "881" && kind !== "industry") setKind("industry");
    else if (want.slice(2, 5) === "880" && kind !== "concept") setKind("concept");
    // 刻意不用 useActiveInterval：这是「等榜单到位后自动选中目标板块」的等待轮询，
    // 命中或 6s 超时即自停。若随 Pane 隐藏而停表，用户切回来会发现目标永远没被选中。
    const t = setInterval(() => {
      const cur = rowsRef.current;
      if (!cur.length) return;                       // 榜单未到位，继续等
      const hit = want
        ? cur.find((r) => r.code === want)
        : cur.find((r) => r.name && r.name.includes(wantName));
      if (hit) { selectBoard(hit.code, hit.name, hit.kind); clearInterval(t); }
      else if (!want && wantName) clearInterval(t); // 名称未命中不再等待
    }, 400);
    setTimeout(() => clearInterval(t), 6000); // 最多等 6s
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  /* ---------- 板块详情：成分股（翻页取全量）+ K 线 ---------- */
  const selectBoard = (code, name, bdKind) => {
    // C1：数据源选择经 ref 读取（深链 interval 闭包不随渲染更新，直读 state 会取旧值）
    const src = srcRef.current;
    setSelCode(code); setSelName(name || code);
    setCons(null); setBars([]); setDetailErr(""); setConsLoading(false);
    setMf(null); setMfLoading(false);
    // 统计类板块（家数口径，如 880075 北证涨跌）无成分股，直接标注，不拉成分。
    if (bdKind === "stat") {
      setCons({ code, total: 0, items: [], page: 0, page_size: 0,
                has_more: false, page_count: 0, stat: true });
      return;
    }
    // 成分股翻页取全量（P0-3）：先取首页立刻展示，再后台补齐剩余页，避免只显示前 50 只。
    setConsLoading(true);
    (async () => {
      try {
        const pageSize = 200;
        const first = await api.marketBoardConstituents({ code, limit: pageSize, page: 0, source: src });
        let acc = first.items || [];
        let hasMore = first.has_more;
        setCons({ code, total: first.total ?? acc.length, items: acc,
                  page: 0, page_size: pageSize, has_more: hasMore,
                  page_count: first.page_count });
        let p = 1;
        while (hasMore && p < 60) {
          const r = await api.marketBoardConstituents({ code, limit: pageSize, page: p, source: src });
          const its = r.items || [];
          acc = acc.concat(its);
          hasMore = r.has_more;
          setCons({ code, total: first.total ?? acc.length, items: acc,
                    page: p, page_size: pageSize, has_more: hasMore,
                    page_count: r.page_count });
          if (!its.length) break;
          p += 1;
        }
      } catch (e) {
        setDetailErr(e.message || "成分股获取失败");
      } finally {
        setConsLoading(false);
      }
    })();
    api.marketBoardKline({ code, period: "1d", count: KLINE_COUNT, source: src })
      .then((r) => setBars(r.bars || []))
      .catch(() => { /* K 线失败不影响成分股展示 */ });
    // G2 板块资金流：成分股当日主力净流入聚合（真实内外盘口径）
    setMfLoading(true);
    api.boardMoneyflow({ code, top_n: 15, source: src })
      .then((r) => setMf(r))
      .catch(() => setMf({ error: "资金流获取失败" }))
      .finally(() => setMfLoading(false));
  };

  const klineOption = useMemo(() => {
    if (!bars.length) return { title: { text: "板块 K 线加载中…", left: "center", top: "center", textStyle: { color: "#8aa0c0" } } };
    const dates = bars.map((b) => b.time);
    const closes = bars.map((b) => Number(b.close));
    // B2：源层板块 K 线 volume 即 volume_lots（手），补成交量子图并带单位图例/tooltip，
    // 与 MarketData 个股 K 线同口径，避免把「手」误读为「股/万元」。
    const vols = bars.map((b) => Number(b.volume) || 0);
    const volColors = bars.map((b) => Number(b.close) >= Number(b.open) ? PALETTE.up : PALETTE.down);
    return {
      animation: false,
      tooltip: { trigger: "axis" },
      legend: { data: ["板块指数", "成交量(手)"], top: 2, textStyle: { color: "#8a97ad", fontSize: 11 }, itemWidth: 16, itemHeight: 8 },
      grid: [
        { left: 56, right: 12, top: 24, height: "62%" },
        { left: 56, right: 12, top: "78%", height: "16%" },
      ],
      xAxis: [
        { type: "category", data: dates, boundaryGap: true, axisLine: { lineStyle: { color: PALETTE.axis } },
          axisLabel: { show: false }, splitLine: { show: false }, axisTick: { show: false } },
        { type: "category", gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: PALETTE.axis } },
          axisLabel: { color: "#5a6a82", fontSize: 10 }, splitLine: { show: false }, axisTick: { show: false } },
      ],
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: PALETTE.split } }, axisLabel: { color: PALETTE.textDim, fontSize: 10 } },
        { gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      series: [
        { name: "板块指数", type: "line", data: closes, showSymbol: false,
          lineStyle: { width: 1.5, color: closes[closes.length - 1] >= closes[0]
            ? (chartSpec?.candlestick?.up || PALETTE.up) : (chartSpec?.candlestick?.down || PALETTE.down) },
          areaStyle: { color: "rgba(79,140,255,.10)" } },
        { name: "成交量(手)", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols,
          itemStyle: (p) => ({ color: volColors[p.dataIndex] }) },
      ],
    };
  }, [bars]);

  const sel = rows.find((r) => r.code === selCode);
  const isStat = sel?.kind === "stat";
  // 头部指数/板块实时：订阅选中板块代码（统计类无连续行情，不订阅）
  const { quotes: headQuotes } = useQuotes(selCode && !isStat ? [selCode] : []);
  const headQ = selCode ? headQuotes[selCode] : null;
  const selLast = headQ?.last != null ? Number(headQ.last) : (sel?.last != null ? Number(sel.last) : null);
  const selPct = headQ?.change_pct != null ? Number(headQ.change_pct) : sel?.change_pct;

  return (
    <div className="page boards-page">
      {/* 工具栏：分类 tab + 排序 + 刷新 + 数据源 */}
      <div className="bd-toolbar">
        <div className="bd-tabs">
          {KIND_TABS.map((k) => (
            <button key={k.v} className={`bd-tab ${kind === k.v ? "active" : ""}`}
              onClick={() => setKind(k.v)}>{k.label}</button>
          ))}
        </div>
        <select value={sortBy} onChange={(e) => setSortBy(e.target.value)} title="排序">
          {SORTS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        {/* C1：数据源切换（请求透传 source 参数） */}
        <select value={srcSel} onChange={(e) => setSrcSel(e.target.value)} title="行情数据源">
          {SRC_OPTS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        <button className="ghost btn-sm" onClick={() => loadBoards()} disabled={loading}>
          {loading ? "加载…" : "刷新"}
        </button>
        {source && <span className="tag ok">数据源：{source}</span>}
        {err && <span className="down">{err}</span>}
      </div>

      <div className="bd-body">
        {/* 左：板块榜单 */}
        <div className="bd-list card">
          <table className="bd-table">
            <thead>
              <tr>
                <th>板块</th><th>代码</th><th className="num">最新</th>
                <th className="num">涨跌幅</th><th className="num">成交额</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const rowStat = r.kind === "stat";
                return (
                <tr key={r.code} className={r.code === selCode ? "sel" : ""}
                  onClick={() => selectBoard(r.code, r.name, r.kind)}>
                  <td>{r.name || "—"}</td>
                  <td className="code">{r.code}</td>
                  <td className="num">{r.last != null
                    ? (rowStat ? `${r.last} 家` : Number(r.last).toFixed(2)) : "—"}</td>
                  <td className={`num ${rowStat ? "muted" : pctCls(r.change_pct)}`}
                    title={rowStat ? "统计类板块：家数口径，非指数点位" : ""}>
                    {r.change_pct != null ? formatPct(r.change_pct) : "—"}</td>
                  <td className="num">{r.amount != null ? formatAmount(r.amount) : "—"}</td>
                </tr>
                );
              })}
              {!rows.length && !loading && (
                <tr><td colSpan={5} className="muted">{err || "暂无板块数据（TDX 行情源未连接）"}</td></tr>
              )}
            </tbody>
          </table>
        </div>

        {/* 右：板块详情（成分股 + 板块 K 线 / 统计类口径说明） */}
        <div className="bd-detail">
          {!selCode && <div className="card muted bd-placeholder">点击左侧板块查看成分股与走势</div>}
          {selCode && isStat && (
            <div className="card bd-stat-note">
              <div className="bd-name">{selName} <span className="code">{selCode}</span></div>
              <div className="bd-stat-metric">
                最新 <b>{sel?.last != null ? `${sel.last} 家` : "—"}</b>
                {selPct != null && <span className="muted">（家数较昨日 {formatPct(selPct)}）</span>}
              </div>
              <p className="muted">统计类板块为市场温度计指标（涨跌家数 / 停板家数等），
                数值是「家数」而非指数点位，仅供市场宽度参考，无成分股与 K 线。</p>
            </div>
          )}
          {selCode && !isStat && (
            <>
              <div className="card bd-head">
                <span className="bd-name">{selName}</span>
                <span className="code">{selCode}</span>
                {selLast != null && <span className={`bd-last ${pctCls(selPct)}`}>{selLast.toFixed(2)}</span>}
                {selPct != null && <span className={`bd-pct ${pctCls(selPct)}`}>{formatPct(selPct)}</span>}
                {cons?.total != null && <span className="muted">成分 {cons.total} 只（实时刷新前 {Math.min(cons.items?.length || 0, 200)}）</span>}
              </div>
              <div className="card bd-chart">
                {linkedInd && (
                  <span className="tag run bd-linked" title="行情页切换指标时联动">
                    联动副图：{String(linkedInd).toUpperCase()}
                  </span>
                )}
                <Chart option={klineOption} height={200} />
              </div>
              <div className="card bd-cons">
                {detailErr && <div className="down">{detailErr}</div>}
                <table className="bd-table">
                  <thead>
                    <tr><th>股票</th><th>代码</th><th className="num">最新</th><th className="num">涨跌幅</th></tr>
                  </thead>
                  <tbody>
                    {(cons?.items || []).map((c) => {
                      const q = consQuotes[c.code];
                      const last = q?.last != null ? Number(q.last) : c.last;
                      const pct = q?.change_pct != null ? Number(q.change_pct) : c.change_pct;
                      return (
                      <tr key={c.code} className="bd-stock"
                        title="点击打开个股分析"
                        onClick={() => navToQuote(c.code)}>
                        <td>{c.name || "—"}</td>
                        <td className="code">{c.code}</td>
                        <td className="num">{last != null ? Number(last).toFixed(2) : "—"}</td>
                        <td className={`num ${pctCls(pct)}`}>
                          {pct != null ? formatPct(pct) : "—"}</td>
                      </tr>
                      );
                    })}
                    {consLoading && (
                      <tr><td colSpan={4} className="muted">成分股加载中…（大板块正在翻页取全量）</td></tr>
                    )}
                    {!consLoading && cons && !cons.items?.length && (
                      <tr><td colSpan={4} className="muted">该板块暂无成分股数据</td></tr>
                    )}
                    {!consLoading && !cons && (
                      <tr><td colSpan={4} className="muted">成分股加载中…</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              {/* G2 板块资金流：成分股当日主力净流入聚合（真实内外盘口径） */}
              <div className="card bd-mf">
                <div className="bd-mf-head">
                  <span>板块资金流（当日主力净流入 · 成分股聚合）</span>
                  {mfLoading && <span className="muted">加载中…</span>}
                </div>
                {mf?.error && <div className="down">{mf.error}</div>}
                {mf && !mf.error && (
                  <>
                    <div className="bd-mf-total">
                      净流入合计 <b className={mf.total_net >= 0 ? "up" : "down"}>
                        {(mf.total_net >= 0 ? "+" : "") + mf.total_net.toLocaleString()} 元</b>
                      <span className="muted">（{mf.count} 只成分 · 口径：当日）</span>
                    </div>
                    <table className="bd-table bd-mf-tbl">
                      <thead><tr><th>股票</th><th>代码</th>
                        <th className="num">净流入(元)</th><th className="num">贡献度</th></tr></thead>
                      <tbody>
                        {(mf.contributors || []).map((c) => (
                          <tr key={c.code} className="bd-stock" onClick={() => navToQuote(c.code)}>
                            <td>{c.name || "—"}</td>
                            <td className="code">{c.code}</td>
                            <td className={`num ${c.net >= 0 ? "up" : "down"}`}>
                              {(c.net >= 0 ? "+" : "") + c.net.toLocaleString()}</td>
                            <td className="num">{c.pct != null ? c.pct.toFixed(2) + "%" : "—"}</td>
                          </tr>
                        ))}
                        {!mf.contributors?.length &&
                          <tr><td colSpan={4} className="muted">无成分资金流数据</td></tr>}
                      </tbody>
                    </table>
                  </>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
