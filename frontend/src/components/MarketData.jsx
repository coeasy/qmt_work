// 行情分析（通达信式）— 单只票的 K 线 / 分时 / 实时 / 五档 / 财务 / F10 / 快速交易 一体化页。
// 合并自原「行情数据 MarketData」+「个股分析 StockAnalysis」。
// - 多周期：分时/1分/5分/15分/30分/60分/日/周/月/季/年
// - 复权：不动/前复权/后复权
// - 指标：MA / BOLL（主图）+ MACD/KDJ/RSI/成交量/WR（副图）
// - 实时：v3 起接 QuoteHub 全局单连接（useQuotes 引用计数订阅），本页不再自建 WS
// - 参数：v3 起由叶子 params 驱动（code/period），外部导航（F3/F4/报价牌点选）直接换参，
//         不再读 sessionStorage —— 多实例 tab 各看各的票，互不串台
// - 布局：v3 起去掉右侧 280px 固定栏，图表全宽 + 底部可折叠抽屉（五档/快速交易/F10）
// - 数据源：自动回退 broker → TDX，无连接也能看 K 线/快照/名称
// - 快捷键：F5 切换分时/K线；F10 聚焦 F10
import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";
import QuotePanel from "./QuotePanel.jsx";
import { quickTradeNavigate } from "../lib/trade.js";
import { navTo, navToQuote } from "../lib/nav.js";
import { sendOrder, precheckOrder } from "../lib/tradeApi.js";
import { useKline, useFundamentals, formatPct, preCloseOf, applyTickToBars } from "../hooks/useMarket.js";
import { useQuotes } from "../lib/quoteHub.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
// G2-3：指标单一真源——前端不再自带指标计算，统一消费后端引擎
import { fetchIndicator, indicatorKey } from "../lib/indicators.js";
// G4 数据面：资金流/股本 topic 总线
import { subscribe as hubSubscribe, invalidate } from "../lib/dataHub.js";
// 金额格式化直接引唯一实现（不再经 useMarket 别名中转，链路更短更明确）
import { fmtAmount } from "../lib/format.js";

// 旧版遗留的悬空引用（fmtPct 未定义，涨跌幅一渲染就会 ReferenceError）——根治为别名
const fmtPct = formatPct;

/* ======================== 常量 ======================== */
// TDX 同款周期：分时 + 1/5/15/30/60 分 + 日/周/月/季/年。
const PERIODS = [
  { v: "tick", label: "分时", kind: "tick" },
  { v: "1m",  label: "1分",  kind: "minute" },
  { v: "5m",  label: "5分",  kind: "minute" },
  { v: "15m", label: "15分", kind: "minute" },
  { v: "30m", label: "30分", kind: "minute" },
  { v: "60m", label: "60分", kind: "minute" },
  { v: "1d",  label: "日线", kind: "kline" },
  { v: "1w",  label: "周线", kind: "kline" },
  { v: "1mo", label: "月线", kind: "kline" },
  { v: "1q",  label: "季线", kind: "kline" },
  { v: "1y",  label: "年线", kind: "kline" },
];
const ADJ_TYPES = [
  { v: "",    label: "不复权" },
  { v: "qfq", label: "前复权" },
  { v: "hfq", label: "后复权" },
];
const MAIN_INDICATORS = [
  { v: "ma",   label: "MA" },
  { v: "boll", label: "BOLL" },
  { v: "none", label: "无" },
];
const SUB_INDICATORS = [
  { v: "macd", label: "MACD" },
  { v: "kdj",  label: "KDJ" },
  { v: "rsi",  label: "RSI" },
  { v: "vol",  label: "VOL" },
  { v: "wr",   label: "WR" },
  { v: "none", label: "无" },
];
const MA_PERIODS = [5, 10, 20, 60];
const MA_COLORS = ["#ffffff", "#fdbb30", "#9b7bd8", "#91cc75"]; // 白/黄/紫/绿
const SRC_LABEL = {
  eltdx: "通达信(TDX)行情",
  broker: "券商",
  cache: "本地缓存",
  cache_stale: "本地缓存(过期)",
};
const DEFAULT_VOLUME = 100;     // 默认委托手数
const PRICE_TICKS = [
  { v: "limit_up", label: "涨停价" },
  { v: "ask1",     label: "卖一价" },
  { v: "last",     label: "最新价" },
  { v: "bid1",     label: "买一价" },
  { v: "limit_dn", label: "跌停价" },
];

/* ======================== 工具函数 ======================== */
function toDate(s) {
  if (!s) return "";
  return String(s).slice(0, 10);
}

// G2-3：本地 7 个指标实现（calcMA/calcEMA/calcMACD/calcKDJ/calcRSI/calcBOLL/calcWR）
// 已删除——指标计算收敛到后端统一引擎（app/indicators，契约逐位一致），
// 本组件经 ../lib/indicators.js 消费 /market/indicators/calc 结果。

/* ======================== 主组件 ======================== */
export default function MarketData({ params, leafId, tabId, dispatch } = {}) {
  const { activeId } = useBroker();
  // 代码以叶子 params 为单一真相；本地 code 仅为渲染缓存（用户在页内订阅新代码时
  // 通过 dispatch LEAF_PARAMS 回写 store，tab 标题随之更新）
  const paramCode = params && typeof params.code === "string" && params.code.trim()
    ? params.code.trim() : null;
  const paramPeriod = params && typeof params.period === "string" ? params.period : null;
  const [code, setCode] = useState(paramCode || "600519.SH");
  const [input, setInput] = useState(paramCode || "600519.SH");
  const [period, setPeriod] = useState(paramPeriod && PERIODS.some((p) => p.v === paramPeriod) ? paramPeriod : "1d");
  const [adj, setAdj] = useState("");
  const [count, setCount] = useState(180);
  const [tick, setTick] = useState(null);
  // 局部"被 tick 就地改写过的 bars"：以 hook 输出为基底，WS tick 到达时改最后一根
  const [barsOverlay, setBarsOverlay] = useState([]);
  // 真实 bars/loading/err/meta/stockInfo/financial 全部由 useKline / useFundamentals 产出
  const [mainInd, setMainInd] = useState("ma");
  const [subInd, setSubInd] = useState("macd");
  // G2-3：后端指标计算结果缓存（key = indicatorKey(name, params) → outputs）
  const [indData, setIndData] = useState({});
  // G9-2 画线工具：水平线（点击 K 线取价位），localStorage 按 code 持久化
  const [drawMode, setDrawMode] = useState(false);
  const [drawings, setDrawings] = useState([]);
  const DRAW_KEY = `qmt_drawings_${code}`;
  useEffect(() => {
    try { setDrawings(JSON.parse(localStorage.getItem(DRAW_KEY) || "[]") || []); }
    catch { setDrawings([]); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [DRAW_KEY]);
  useEffect(() => {
    try { localStorage.setItem(DRAW_KEY, JSON.stringify(drawings)); } catch { /* ignore */ }
  }, [drawings, DRAW_KEY]);
  const clearDrawings = () => setDrawings([]);

  // 底部抽屉：五档盘口 | 快速交易 | F10
  const [drawerTab, setDrawerTab] = useState("quote");
  const [drawerOpen, setDrawerOpen] = useState(true);

  // 快速交易
  const [qtDir, setQtDir] = useState("buy");
  const [qtVol, setQtVol] = useState(DEFAULT_VOLUME);
  const [qtPriceKind, setQtPriceKind] = useState("ask1");
  const [qtPrice, setQtPrice] = useState("");
  const [qtSending, setQtSending] = useState(false);
  const [qtMsg, setQtMsg] = useState("");
  const [qtMsgType, setQtMsgType] = useState(""); // ok|err|info
  // I2 同板块联动：当前票的所属行业/概念板块内其他成分股（实时）
  const [linkName, setLinkName] = useState("");
  const [linkCode, setLinkCode] = useState(null);
  const [linkCons, setLinkCons] = useState(null);
  const [linkLoading, setLinkLoading] = useState(false);

  const lastSubmitRef = useRef(0); // 防双击 5s

  // 外部导航换票（F3/F4/报价牌/命令面板 → replace 参数）：就地切换，不丢分屏与指标状态
  useEffect(() => {
    if (!paramCode || paramCode === code) return;
    setCode(paramCode);
    setInput(paramCode);
    setTick(null);
    setBarsOverlay([]);
    setQtPrice(""); setQtMsg(""); setQtMsgType("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramCode]);

  /* ---------- 周期契约（后端单一真相，P0-1） ----------
   * 周期条改为契约驱动：由 /market/periods 决定「有哪些周期、哪些可用」。
   * 后端不可用时回退本地 PERIODS（仅 UI 兜底），但一旦拿到服务端清单即以之为准。
   * 这样后端下线某周期（如季线无数据源）时前端自动置灰，而不是让用户点到错的图上。 */
  const [srvPeriods, setSrvPeriods] = useState(null);
  useEffect(() => {
    let alive = true;
    api.marketPeriods()
      .then((r) => {
        if (alive && Array.isArray(r && r.periods) && r.periods.length) setSrvPeriods(r.periods);
      })
      .catch(() => { /* 后端不可用 → 保持 null，用本地兜底 */ });
    return () => { alive = false; };
  }, []);
  const periodList = srvPeriods || PERIODS;

  // 若当前周期被契约判定为不可用（如季线无数据源），自动落回日线，避免停在错误周期上。
  useEffect(() => {
    if (!srvPeriods) return;
    const cur = srvPeriods.find((p) => p.v === period);
    if (cur && cur.supported === false) setPeriod("1d");
  }, [srvPeriods, period]);

  const periodMeta = useMemo(
    () => periodList.find((p) => p.v === period) || { v: period, label: period, kind: "kline" },
    [periodList, period],
  );

  /* ---------- 统一数据获取：useKline / useFundamentals ---------- */
  const klineQ = useKline({ code, period, count, adj, connId: activeId });
  const fundQ = useFundamentals(code, activeId);

  // 合并：hook 输出的 bars 为基底，barsOverlay（被 tick 就地改写过）覆盖
  const baseBars = klineQ.data?.bars || [];
  const bars = barsOverlay.length >= baseBars.length ? barsOverlay : baseBars;

  // 当 baseBars 出现新数据（period/count/adj/code 变更）时，重置 overlay
  useEffect(() => { setBarsOverlay([]); /* eslint-disable-next-line */ }, [code, period, count, adj]);

  /* ---------- G2-3：后端指标（单一真源）——按主/副图需要并行拉取，带缓存 ----------
     指标计算已收敛到后端引擎（app/indicators），此处仅消费 /market/indicators/calc。
     依赖用 bars.length + 显式维度，避免 tick 就地改写 barsOverlay 触发的无谓重拉；
     后端不可用时该叠加层不渲染（绝不本地重算，避免重新制造双份实现）。 */
  useEffect(() => {
    if (!bars.length) return;
    const need = [];
    if (mainInd === "ma") {
      MA_PERIODS.forEach((p) => need.push(["ma", { win: p }]));
    } else if (mainInd === "boll") {
      need.push(["boll", {}]);
    }
    if (subInd === "macd") need.push(["macd", {}]);
    else if (subInd === "kdj") need.push(["kdj", {}]);
    else if (subInd === "rsi") { need.push(["rsi", { win: 6 }]); need.push(["rsi", { win: 12 }]); }
    else if (subInd === "wr") need.push(["wr", {}]);
    let alive = true;
    const base = { code, period, count, adj, source: "auto" };
    Promise.all(need.map(([name, indParams]) =>
      fetchIndicator(name, { ...base, ...indParams }).then((outs) => [name, indParams, outs])))
      .then((rows) => {
        if (!alive) return;
        const map = {};
        rows.forEach(([name, indParams, outs]) => { if (outs) map[indicatorKey(name, indParams)] = outs; });
        setIndData(map);
      });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars.length, code, period, count, adj, mainInd, subInd]);

  /* ---------- G9-2 画线：画线模式点击 K 线取价 → 水平线（convertToPixel 像素存储） ---------- */
  useEffect(() => {
    const inst = chartRef.current && chartRef.current.getInstance
      ? chartRef.current.getInstance() : null;
    if (!drawMode || !inst) return;
    const onClick = (p) => {
      if (!p || p.value == null) return;
      const price = Array.isArray(p.value) ? Number(p.value[1]) : Number(p.value);
      if (Number.isNaN(price)) return;
      const y = inst.convertToPixel({ yAxisIndex: 0 }, price);
      if (y == null) return;
      setDrawings((prev) => [...prev.slice(-19), { id: Date.now(), price, y }]);
    };
    inst.on("click", onClick);
    return () => inst.off("click", onClick);
  }, [drawMode, code, period, bars.length]);

  const meta = klineQ.data && klineQ.data.source ? {
    source: klineQ.data.source,
    cached_at: klineQ.data.cached_at,
    note: klineQ.data.note,
    count: klineQ.data.count,
  } : null;
  const loading = klineQ.loading;
  const err = klineQ.err;

  // C1：十字光标 OHLC 信息条 —— 引用最新 bars / 是否 K 线，供图表事件回调读取
  const chartRef = useRef(null);
  const [ohlc, setOhlc] = useState(null); // { date, open, high, low, close, prev, pct }
  const barsRef = useRef(bars); barsRef.current = bars;
  const isKlineRef = useRef(true); isKlineRef.current = periodMeta.kind !== "tick";

  // stockInfo / financial 从 hook 解构
  const stockInfo = fundQ.info;
  const financial = fundQ.fin;

  /* ---------- 多维摘要数据（I1，东财对标）：资金流 + 流通股本/涨跌停 ----------
   * 真实口径（/market/moneyflow est=false，/market/capital）；code 变更拉一次 +
   * 手动刷新，不轮询（J2）；任一字段缺失显示「—」，绝不估算填充。 */
  const [mf, setMf] = useState(null);          // /market/moneyflow：net/inside/outside/strength/volume_ratio
  const [mfErr, setMfErr] = useState("");
  const [cap, setCap] = useState(null);        // /market/capital：shares + limits
  const [mfRefresh, setMfRefresh] = useState(0);
  // G4 数据面：资金流/股本走 topic 总线（切回已看代码秒出快照，同页不重复请求）
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

  /* ---------- C2：资金流日内回放（G3 落库观测的快照序列） ----------
   * 打开「多维摘要」抽屉时拉一次（不轮询）；无快照显「暂无快照」引导，
   * 数据来自 moneyflow_cache 真实落库，非估算。 */
  const [replay, setReplay] = useState(null);
  const [replayErr, setReplayErr] = useState("");
  useEffect(() => {
    if (drawerTab !== "mf" || !code) return;
    let alive = true;
    setReplay(null); setReplayErr("");
    api.moneyflowReplay({ code, limit: 240 })
      .then((r) => { if (alive) setReplay(r); })
      .catch((e) => { if (alive) setReplayErr(e.message || "回放数据暂不可用"); });
    return () => { alive = false; };
  }, [drawerTab, code, mfRefresh]);
  const replayOption = useMemo(() => {
    const rows = (replay?.rows || []).filter((r) => r.net != null);
    if (rows.length < 2) return null;
    return {
      animation: false,
      grid: { left: 64, right: 12, top: 10, bottom: 22 },
      xAxis: { type: "category", data: rows.map((r) => (r.ts || "").slice(11, 16)),
        axisLabel: { color: "#5a6a82", fontSize: 10 }, axisLine: { lineStyle: { color: "#3a4a66" } } },
      yAxis: { scale: true, splitLine: { lineStyle: { color: "rgba(58,74,102,.3)" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 } },
      tooltip: { trigger: "axis" },
      series: [{
        name: "净流入(手)", type: "line", data: rows.map((r) => Number(r.net)), showSymbol: false,
        lineStyle: { width: 1.5, color: rows[rows.length - 1].net >= 0 ? "#ef4d56" : "#29c08a" },
        areaStyle: { color: "rgba(79,140,255,.10)" },
      }],
    };
  }, [replay]);

  // "刷新"按钮：调一次 force，再触发 hook 重新跑（force 参数会被 hook loader 忽略，
  // 这里同时刷新本地缓存 + hook reload，达到 TDX 同款"重拉服务端"效果）
  const forceKline = useCallback(() => {
    if (!code) return;
    api.marketKline({ code, period, count, conn_id: activeId, adj, force: true })
      .catch(() => {})
      .finally(() => klineQ.reload());
  }, [code, period, count, adj, activeId, klineQ]);

  /* ---------- 订阅（切换代码） ---------- */
  const subscribe = useCallback(() => {
    const sym = input.trim();
    if (!sym) return;
    setCode(sym);
    setTick(null); setBarsOverlay([]);
    setQtPrice(""); setQtMsg(""); setQtMsgType("");
    // 回写叶子参数：tab 标题跟随代码，刷新/重开也能恢复
    if (dispatch && tabId && leafId) {
      dispatch({ type: "LEAF_PARAMS", tabId, leafId, params: { code: sym } });
    }
  }, [input, dispatch, tabId, leafId]);

  /* ---------- 应用实时报价（QuoteHub tick） ---------- */
  const applyQuote = useCallback((q) => {
    if (!q) return;
    setTick(q);
    setBarsOverlay((prev) => applyTickToBars(prev.length ? prev : baseBars, q));
  }, [baseBars]);

  /* ---------- 真分时（TDX 同款）：/market/minutes 分钟曲线，60s 轮询跟随 ---------- */
  const [minutesData, setMinutesData] = useState(null);
  const [minutesErr, setMinutesErr] = useState("");
  const minutesCodeRef = useRef(code);   // 分时请求回填校验：防 code 切换后旧响应覆盖新数据
  // 非分时/无代码：清空（保留原语义）
  useEffect(() => {
    if (period !== "tick" || !code) { setMinutesData(null); setMinutesErr(""); return; }
    minutesCodeRef.current = code;
  }, [period, code]);
  // G5：60s 跟随刷新，后台 Tab 停表；切回前台立即拉一次
  // minutesCodeRef 用于防止 code 切换后旧请求回填覆盖新数据（原 alive 标志的等价物）
  useActiveInterval(
    () => {
      api.marketMinutes({ code })
        .then((d) => { if (minutesCodeRef.current === code) { setMinutesData(d); setMinutesErr(""); } })
        .catch((e) => { if (minutesCodeRef.current === code) setMinutesErr(e.message || "分时不可用"); });
    },
    period === "tick" && code ? 60000 : 0,
    [period, code],
    { immediate: period === "tick" && !!code },
  );

  /* ---------- QuoteHub 全局单连接订阅（替代本页自建 WS） ---------- */
  const { quotes, state: hubState } = useQuotes([code]);

  // I2 同板块联动：订阅板块成分股（前 200 只）实时价，离开/切换自动退订（延迟 60s 宽限）。
  const linkCodes = useMemo(
    () => (linkCons?.items || []).slice(0, 200).map((c) => c.code), [linkCons]);
  const { quotes: linkQuotes } = useQuotes(linkCodes);
  // 经 /market/board/lookup 把行业/概念名称解析为确切板块代码，再取成分股（F3 稳化链路复用）。
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
  // 行业名需在 I2 联动 effect 之前声明，避免 const TDZ（依赖数组读取 industryName）
  const industryName = stockInfo?.industry || null;
  // 切换股票/行业时自动联动到所属行业板块
  useEffect(() => {
    if (industryName) loadLink(industryName);
    else { setLinkCode(null); setLinkCons(null); setLinkName(""); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, industryName]);
  const hubQuote = quotes[code];
  useEffect(() => { if (hubQuote) applyQuote(hubQuote); }, [hubQuote, applyQuote]);

  /* ---------- F5 切分时↔K线 ---------- */
  useEffect(() => {
    const onToggle = () => setPeriod((p) => (p === "tick" ? "1d" : "tick"));
    window.addEventListener("sa:toggle-period", onToggle);
    return () => window.removeEventListener("sa:toggle-period", onToggle);
  }, []);

  /* ---------- F10 聚焦闪烁 ---------- */
  useEffect(() => {
    const onFocus = () => {
      const el = document.querySelector(".sa-f10");
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("sa-f10-flash");
        setTimeout(() => el.classList.remove("sa-f10-flash"), 1200);
      }
    };
    window.addEventListener("sa:focus-f10", onFocus);
    return () => window.removeEventListener("sa:focus-f10", onFocus);
  }, []);

  /* ---------- 派生：TDX 同款色块（红涨绿跌） ---------- */
  const last = tick?.last != null ? Number(tick.last) : null;
  const pre = preCloseOf(tick, stockInfo);
  const pct = last != null && pre ? ((last - pre) / pre) * 100 : null;
  const chg = last != null && pre ? last - pre : null;
  const cls = pct == null ? "" : pct >= 0 ? "up" : "down";
  const name = stockInfo?.name || tick?.name || code;
  const lastBar = bars[bars.length - 1];

  /* ---------- 派生：多维摘要口径（真实数据，缺失显「—」） ---------- */
  const circShares = cap?.shares?.[code]?.circulating_shares != null
    ? Number(cap.shares[code].circulating_shares) : null;
  // 换手率 = 成交量(手)×100 / 流通股本(股) × 100%
  const turnover = tick?.volume != null && circShares
    ? (Number(tick.volume) * 100 / circShares) * 100 : null;
  const mfNet = mf?.net != null ? Number(mf.net) : null;
  const mfInside = mf?.inside != null ? Number(mf.inside) : null;
  const mfOutside = mf?.outside != null ? Number(mf.outside) : null;
  const limitInfo = cap?.limits?.[code] || null;
  const conceptList = Array.isArray(stockInfo?.concepts) ? stockInfo.concepts : [];
  // 深链板块页（F3 联动）：先经 /market/board/lookup 把名称解析为确切板块代码，
  // 再带 code 跳转（后端板块榜按 code 精确选中），避免 name.includes 误匹配 /
  // 同名板块命中错项 / 榜单未加载时静默失败（P1-8）。
  const toBoards = async (nm) => {
    try {
      const r = await api.marketBoardLookup({ name: nm, limit: 1 });
      const m = (r && r.matches && r.matches[0]) || null;
      if (m && m.code) {
        navTo("boards", { params: { code: m.code, name: m.name || nm } });
        return;
      }
    } catch { /* 降级：按名称深链 */ }
    navTo("boards", { params: { name: nm } });
  };

  // 快速交易价格档
  const priceOfKind = (kind) => {
    if (!last && !pre) return null;
    if (kind === "limit_up") return stockInfo?.high_limit != null ? Number(stockInfo.high_limit)
      : (tick?.high_limit != null ? Number(tick.high_limit) : null);
    if (kind === "limit_dn") return stockInfo?.low_limit != null ? Number(stockInfo.low_limit)
      : (tick?.low_limit != null ? Number(tick.low_limit) : null);
    if (kind === "ask1") return tick?.asks?.[0]?.price != null ? Number(tick.asks[0].price) : (last != null ? last : null);
    if (kind === "bid1") return tick?.bids?.[0]?.price != null ? Number(tick.bids[0].price) : (last != null ? last : null);
    if (kind === "last") return last != null ? last : null;
    return null;
  };
  // 自动跟随 tick 同步默认价格
  useEffect(() => {
    const p = priceOfKind(qtPriceKind);
    if (p != null) setQtPrice(String(p.toFixed(2)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qtPriceKind, tick?.last, tick?.asks?.[0]?.price, tick?.bids?.[0]?.price, stockInfo?.high_limit, stockInfo?.low_limit]);

  /* ---------- 快速交易提交（含预检 + 幂等 + 字段归一化） ---------- */
  const submitOrder = useCallback(async () => {
    setQtMsg(""); setQtMsgType("");
    if (!activeId) { setQtMsg("未连接券商：到「券商连接」添加后再下单"); setQtMsgType("err"); return; }
    if (!code) { setQtMsg("代码未填"); setQtMsgType("err"); return; }

    // 防双击 5s 窗口（与后端 single_flight 同窗口）
    const now = Date.now();
    if (now - lastSubmitRef.current < 5000) {
      setQtMsg("操作过快，请稍后再试"); setQtMsgType("err"); return;
    }

    setQtSending(true);
    try {
      // 1) 预检（在用户看得到的位置给出风控预警，TDX 同款"预演"）
      const pc = await precheckOrder({
        code, direction: qtDir, volume: qtVol, price: qtPrice,
        price_type: "limit", conn_id: activeId,
      });
      // 系统错误（无券商/网络）：直接显示并返回，不再尝试真下单
      if (!pc.ok) {
        setQtMsg("预检失败：" + pc.reason); setQtMsgType("err");
        lastSubmitRef.current = now; return;
      }
      // 风控拒绝：明确告知并退出
      if (pc.allowed === false) {
        setQtMsg("风控拦截：" + (pc.reason || "未通过风控预检")); setQtMsgType("err");
        lastSubmitRef.current = now; return;
      }
      // 2) 真下单（tradeApi.sendOrder 内部归一化 direction / 幂等键 / 错误归一化）
      const d = await sendOrder({
        code, direction: qtDir, volume: qtVol, price: qtPrice,
        price_type: "limit", conn_id: activeId,
      });
      setQtMsg(`已报：${d?.order_id || "（等待回报）"}`);
      setQtMsgType("ok");
      lastSubmitRef.current = now;
    } catch (e) {
      setQtMsg(e.message || "提交失败");
      setQtMsgType("err");
    } finally {
      setQtSending(false);
    }
  }, [activeId, code, qtDir, qtPrice, qtVol]);

  /* ======================== 图：真分时（通达信同款） ======================== */
  // 数据：/market/minutes 分钟曲线（eltdx TDX 分时，含均价线）；
  // 实时：QuoteHub tick 就地改写最后一个点，60s 轮询整体刷新。
  // 无分时数据时回退为快照基准的占位提示（绝不伪造曲线）。
  const tickOption = useMemo(() => {
    const preC0 = preCloseOf(tick, stockInfo);
    const pts = minutesData?.points || [];
    if (!pts.length) {
      return {
        title: {
          text: minutesErr ? `分时不可用：${minutesErr}` : "分时加载中…（TDX 分时源）",
          left: "center", top: "center", textStyle: { color: "#8aa0c0" },
        },
      };
    }
    // 昨收基准：分时接口自带优先，回退快照
    const preC = minutesData?.pre_close != null ? Number(minutesData.pre_close)
      : (preC0 != null ? Number(preC0) : (pts[0]?.price != null ? Number(pts[0].price) : null));
    // 均价线：eltdx 缺 avg_price 时按 累计额/累计量 自算（TDX 均价定义）
    let cumAmt = 0, cumVol = 0;
    const priceData = pts.map((p, i) => {
      const px = Number(p.price);
      const v = Number(p.volume) || 0;
      if (p.avg != null) return Number(p.avg);
      cumAmt += px * v; cumVol += v;
      return cumVol > 0 ? cumAmt / cumVol : px;
    });
    const labels = pts.map((p) => p.t || "");
    // 实时 tick 覆盖最后一点（无 tick 时保持 REST 数据）
    if (tick?.last != null) priceData[priceData.length - 1] = Number(tick.last);
    const priceArr = pts.map((p) => Number(p.price));
    // 分钟量配色：相对前一分钟涨红跌绿（TDX 分时量柱同款）
    const volData = pts.map((p, i) => {
      const prev = i > 0 ? priceArr[i - 1] : (preC != null ? preC : priceArr[i]);
      return { value: Number(p.volume) || 0, itemStyle: { color: priceArr[i] >= prev ? "#ef4d56" : "#29c08a" } };
    });
    // 价格域：曲线 + 均价 + 昨收，上下留 8% 缓冲
    let lo = Math.min(...priceArr, ...priceData.filter((v) => v != null), preC ?? Infinity);
    let hi = Math.max(...priceArr, ...priceData.filter((v) => v != null), preC ?? -Infinity);
    if (!isFinite(lo)) lo = 0; if (!isFinite(hi)) hi = 1;
    const pad = (hi - lo) * 0.08 || hi * 0.001 || 1;
    lo -= pad; hi += pad;
    const yMin = Number(lo.toFixed(2)), yMax = Number(hi.toFixed(2));
    // 右侧涨跌%轴：与价格轴同域换算（TDX 双轴同款）
    const toPct = (v) => preC ? ((v - preC) / preC) * 100 : 0;
    const upNow = (priceArr[priceArr.length - 1] ?? preC ?? 0) >= (preC ?? 0);
    const lineColor = upNow ? "#ef4d56" : "#29c08a";
    return {
      animation: false,
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", axisPointer: { type: "cross",
          label: { backgroundColor: "#2c3850", borderColor: "#2c3850", color: "#e6ecf5", fontSize: 11 } },
        backgroundColor: "rgba(18,22,32,.92)", borderColor: "#2c3850", textStyle: { color: "#e6ecf5", fontSize: 12 } },
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#2c3850", borderColor: "#2c3850", color: "#e6ecf5", fontSize: 11 } },
      legend: { data: ["价格", "均价"], top: 2, textStyle: { color: "#8a97ad", fontSize: 11 }, itemWidth: 16, itemHeight: 8 },
      grid: [
        { left: 62, right: 52, top: 24, height: "56%" },
        { left: 62, right: 52, top: "76%", height: "14%" },
      ],
      xAxis: [
        { type: "category", data: labels, boundaryGap: false,
          axisLine: { lineStyle: { color: "#2c3850" } },
          axisLabel: { color: "#5a6a82", fontSize: 10, interval: Math.max(1, Math.floor(labels.length / 6)) },
          splitLine: { show: true, lineStyle: { color: "#1a2233", type: "dashed" } }, axisTick: { show: false } },
        { type: "category", gridIndex: 1, data: labels, boundaryGap: false,
          axisLine: { lineStyle: { color: "#2c3850" } },
          axisLabel: { color: "#5a6a82", fontSize: 10, interval: Math.max(1, Math.floor(labels.length / 6)) },
          axisTick: { show: false }, splitLine: { show: false } },
      ],
      yAxis: [
        { scale: true, min: yMin, max: yMax, position: "left",
          axisLine: { lineStyle: { color: "#2c3850" } },
          axisLabel: { color: "#5a6a82", fontSize: 10 },
          splitLine: { lineStyle: { color: "#1a2233", type: "dashed" } } },
        // 右侧涨跌% 轴（与价格轴同 min/max 域换算）
        { scale: true, min: yMin, max: yMax, position: "right", gridIndex: 0,
          axisLabel: { color: "#5a6a82", fontSize: 10,
            formatter: (v) => (preC ? ((toPct(v) >= 0 ? "+" : "") + toPct(v).toFixed(2) + "%") : "") },
          splitLine: { show: false }, axisLine: { show: false } },
        { gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false }, axisTick: { show: false } },
      ],
      series: [
        { name: "价格", type: "line", data: priceArr, smooth: false, showSymbol: false,
          lineStyle: { width: 1.4, color: lineColor },
          areaStyle: { color: upNow ? "rgba(239,77,86,.14)" : "rgba(41,192,138,.14)" },
          markLine: preC ? { symbol: "none", silent: true,
            lineStyle: { color: "#8aa0c0", type: "dashed", width: 1 },
            data: [{ yAxis: preC, label: { formatter: `昨收 ${preC.toFixed(2)}`, color: "#8aa0c0", fontSize: 10 } }] } : undefined,
        },
        { name: "均价", type: "line", data: priceData, smooth: false, showSymbol: false,
          lineStyle: { width: 1, color: "#fdbb30" } },
        // B2：源层个股/板块 K 线 volume 均为 volume_lots（手），图例带单位防误读
        { name: "分钟量(手)", type: "bar", xAxisIndex: 1, yAxisIndex: 2, data: volData },
      ],
    };
  }, [minutesData, minutesErr, tick, stockInfo]);

  /* ======================== 图：K 线 + 主/副指标 ======================== */
  const klineOption = useMemo(() => {
    if (!bars.length) {
      return { title: { text: err ? "加载失败，请检查代码或券商连接" : "暂无数据，输入代码后点击订阅", left: "center", top: "center", textStyle: { color: "#8aa0c0" } } };
    }
    const times = bars.map((b) => toDate(b.time || b.date));
    const candle = bars.map((b) => [Number(b.open), Number(b.close), Number(b.low), Number(b.high)]);
    const vols = bars.map((b) => Number(b.volume) || 0);
    const volColors = bars.map((b) => Number(b.close) >= Number(b.open) ? "#ef4d56" : "#29c08a");

    const opt = {
      backgroundColor: "transparent",
      animation: false,
      tooltip: { trigger: "axis", axisPointer: { type: "cross",
          label: { backgroundColor: "#2c3850", borderColor: "#2c3850", color: "#e6ecf5", fontSize: 11 } },
        backgroundColor: "rgba(18,22,32,.92)", borderColor: "#2c3850", textStyle: { color: "#e6ecf5", fontSize: 12 } },
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#2c3850", borderColor: "#2c3850", color: "#e6ecf5", fontSize: 11 } },
      grid: [],
      xAxis: [],
      yAxis: [],
      series: [],
      legend: { data: [], top: 2, textStyle: { color: "#8a97ad", fontSize: 11 }, itemWidth: 16, itemHeight: 8 },
    };

    // 底部预留 26px 给 dataZoom 滑块，故三栏较改造前整体压缩
    opt.grid[0] = { left: 55, right: 8, top: 24, height: "50%" };
    opt.xAxis[0] = { type: "category", data: times, boundaryGap: true,
      axisLine: { lineStyle: { color: "#2c3850" } }, axisLabel: { show: false, color: "#5a6a82", fontSize: 10 },
      splitLine: { show: false }, axisTick: { show: false } };
    opt.yAxis[0] = { scale: true, position: "right",
      axisLine: { lineStyle: { color: "#2c3850" } },
      axisLabel: { color: "#5a6a82", fontSize: 10, formatter: "{value}" },
      splitLine: { lineStyle: { color: "#1a2233", type: "dashed" } } };
    opt.series.push({
      name: "K线", type: "candlestick", data: candle,
      itemStyle: { color: "#ef4d56", color0: "#29c08a", borderColor: "#ef4d56", borderColor0: "#29c08a" },
    });

    if (mainInd === "ma") {
      MA_PERIODS.forEach((p, idx) => {
        const ma = indData[indicatorKey("ma", { win: p })] || {};
        opt.legend.data.push(`MA${p}`);
        opt.series.push({ name: `MA${p}`, type: "line", data: ma.ma || [], smooth: true,
          symbol: "none", lineStyle: { width: 1, color: MA_COLORS[idx] } });
      });
    } else if (mainInd === "boll") {
      const b = indData[indicatorKey("boll", {})] || {};
      opt.legend.data.push("BOLL-UP", "BOLL-MID", "BOLL-LOW");
      opt.series.push(
        { name: "BOLL-UP", type: "line", data: b.upper || [], symbol: "none", lineStyle: { width: 1, color: "#c23531" } },
        { name: "BOLL-MID", type: "line", data: b.mid || [], symbol: "none", lineStyle: { width: 1, color: "#91cc75" } },
        { name: "BOLL-LOW", type: "line", data: b.lower || [], symbol: "none", lineStyle: { width: 1, color: "#c23531" } },
      );
    }

    opt.grid[1] = { left: 55, right: 8, top: "70%", height: "11%" };
    opt.xAxis[1] = { type: "category", gridIndex: 1, data: times, boundaryGap: true,
      axisLine: { lineStyle: { color: "#2c3850" } },
      axisLabel: { color: "#5a6a82", fontSize: 10 }, axisTick: { show: false }, splitLine: { show: false } };
    opt.yAxis[1] = { gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { show: false },
      axisTick: { show: false }, splitLine: { show: false } };
    // B2：源层 volume 为 volume_lots（手），图例与 tooltip 同名带单位
    opt.legend.data.push("成交量(手)");
    opt.series.push({
      name: "成交量(手)", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols,
      itemStyle: (p) => ({ color: volColors[p.dataIndex] }),
    });

    if (subInd === "macd") {
      const m = indData[indicatorKey("macd", {})] || {};
      // bottom 用固定像素（而非百分比）：无论图表容器多高，都稳定留出滑块空间
      opt.grid[2] = { left: 55, right: 8, top: "82%", bottom: 26 };
      opt.xAxis[2] = { type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 }, axisTick: { show: false }, splitLine: { show: false } };
      opt.yAxis[2] = { gridIndex: 2, scale: true, splitNumber: 2, axisLabel: { show: false },
        axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("DIF", "DEA", "MACD");
      const mBar = m.bar || [];
      opt.series.push(
        { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: m.dif || [], symbol: "none", lineStyle: { width: 1, color: "#ffffff" } },
        { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: m.dea || [], symbol: "none", lineStyle: { width: 1, color: "#fdbb30" } },
        { name: "MACD", type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: mBar,
          itemStyle: (p) => ({ color: (mBar[p.dataIndex] || 0) >= 0 ? "#ef4d56" : "#29c08a" }) },
      );
    } else if (subInd === "kdj") {
      const k = indData[indicatorKey("kdj", {})] || {};
      // bottom 用固定像素（而非百分比）：无论图表容器多高，都稳定留出滑块空间
      opt.grid[2] = { left: 55, right: 8, top: "82%", bottom: 26 };
      opt.xAxis[2] = { type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 }, axisTick: { show: false }, splitLine: { show: false } };
      opt.yAxis[2] = { gridIndex: 2, scale: true, min: 0, max: 100, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("K", "D", "J");
      opt.series.push(
        { name: "K", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: k.k || [], symbol: "none", lineStyle: { width: 1, color: "#ef4d56" } },
        { name: "D", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: k.d || [], symbol: "none", lineStyle: { width: 1, color: "#fdbb30" } },
        { name: "J", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: k.j || [], symbol: "none", lineStyle: { width: 1, color: "#4f8cff" } },
      );
    } else if (subInd === "rsi") {
      const r6 = (indData[indicatorKey("rsi", { win: 6 })] || {}).rsi || [];
      const r12 = (indData[indicatorKey("rsi", { win: 12 })] || {}).rsi || [];
      // bottom 用固定像素（而非百分比）：无论图表容器多高，都稳定留出滑块空间
      opt.grid[2] = { left: 55, right: 8, top: "82%", bottom: 26 };
      opt.xAxis[2] = { type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 }, axisTick: { show: false }, splitLine: { show: false } };
      opt.yAxis[2] = { gridIndex: 2, scale: true, min: 0, max: 100, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("RSI6", "RSI12");
      opt.series.push(
        { name: "RSI6", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: r6, symbol: "none", lineStyle: { width: 1, color: "#ef4d56" } },
        { name: "RSI12", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: r12, symbol: "none", lineStyle: { width: 1, color: "#4f8cff" } },
      );
    } else if (subInd === "wr") {
      const w = (indData[indicatorKey("wr", {})] || {}).wr || [];
      // bottom 用固定像素（而非百分比）：无论图表容器多高，都稳定留出滑块空间
      opt.grid[2] = { left: 55, right: 8, top: "82%", bottom: 26 };
      opt.xAxis[2] = { type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 }, axisTick: { show: false }, splitLine: { show: false } };
      opt.yAxis[2] = { gridIndex: 2, scale: true, splitNumber: 2, axisLabel: { show: false },
        axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("WR");
      opt.series.push({ name: "WR", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: w,
        symbol: "none", lineStyle: { width: 1, color: "#91cc75" } });
    }

    /* ---------- G9：缩放平移（TDX 标配体验） ----------
       inside  = 滚轮缩放 + 拖拽平移（不占版面）
       slider  = 底部区间条，可拖动两端把手
       两者共用同一组 xAxisIndex，保证主图 / 成交量 / 副图三者联动；
       副图（MACD/KDJ/RSI/WR）缺失时 xAxis 只有 2 条，故按实际条数动态取索引。 */
    const total = times.length;
    // 默认视野：最多显示最近 120 根，避免 180 根挤成一片；数据不足则全显
    const startPct = total > 120 ? Math.max(0, 100 - (120 / total) * 100) : 0;
    const zoomAxes = opt.xAxis.map((_, i) => i);
    opt.dataZoom = [
      { type: "inside", xAxisIndex: zoomAxes, start: startPct, end: 100,
        minValueSpan: 20, zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false },
      { type: "slider", xAxisIndex: zoomAxes, start: startPct, end: 100, minValueSpan: 20,
        bottom: 4, height: 16, borderColor: "#2c3850",
        fillerColor: "rgba(79,140,255,.12)",
        handleStyle: { color: "#4f8cff", borderColor: "#4f8cff" },
        moveHandleStyle: { color: "#3a4a66" },
        dataBackground: { lineStyle: { color: "#3a4a66" }, areaStyle: { color: "rgba(58,74,102,.35)" } },
        selectedDataBackground: { lineStyle: { color: "#4f8cff" }, areaStyle: { color: "rgba(79,140,255,.25)" } },
        textStyle: { color: "#5a6a82", fontSize: 9 } },
    ];
    // G9-2 画线：水平线 graphic 元素（y 为点击时 convertToPixel 像素；缩放后位置近似）
    if (drawings.length) {
      opt.graphic = drawings.map((d) => ({
        type: "line",
        shape: { x1: 0, y1: d.y, x2: 10000, y2: d.y },
        style: { stroke: "#fdbb30", lineWidth: 1, lineDash: [4, 4], opacity: 0.8 },
      }));
    }
    return opt;
  }, [bars, mainInd, subInd, err, indData, drawings]);

  /* ======================== C1：十字光标 OHLC 信息条 ======================== */
  useEffect(() => {
    const inst = chartRef.current?.getInstance();
    if (!inst) return;

    const applyIndex = (di) => {
      const arr = barsRef.current;
      if (!arr || !arr.length) return;
      const idx = di < 0 ? arr.length - 1 : di;
      const b = arr[idx];
      if (!b) return;
      const open = +b.open, close = +b.close, high = +b.high, low = +b.low;
      const prev = idx > 0 ? +arr[idx - 1].close : open;
      const barPct = prev ? ((close - prev) / prev) * 100 : 0;
      setOhlc({ date: toDate(b.time || b.date), open, high, low, close, prev, pct: barPct });
    };

    const onAxis = (ev) => {
      if (!isKlineRef.current) return;
      const info = ev?.axesInfo && ev.axesInfo[0];
      const di = info ? info.dataIndex : ev?.dataIndex;
      if (di == null || di < 0) return;
      applyIndex(di);
    };
    inst.on("updateAxisPointer", onAxis);

    const zr = inst.getZr();
    const onOut = () => applyIndex(barsRef.current.length - 1);
    zr.on("mouseout", onOut);

    // 初始显示最后一根
    applyIndex(barsRef.current.length - 1);

    return () => {
      inst.off("updateAxisPointer", onAxis);
      zr.off("mouseout", onOut);
    };
  }, [klineOption]);

  /* ======================== 渲染 ======================== */
  return (
    <div className="page market-page">
      {/* 顶栏 1：标题 + WS 状态 + 代码/名/价/涨跌 */}
      <div className="mp-header">
        <div className="mp-title-row">
          <h2 className="page-title">行情分析</h2>
          <span className={`ws-badge ws-${hubState}`}>
            {hubState === "connected" ? "● 已连接"
              : hubState === "reconnecting" ? "⟳ 重连…"
              : hubState === "connecting" ? "○ 连接…" : "○ 离线"}
          </span>
          <span className="mp-stock">
            <span className="mp-code">{code}</span>
            <span className="mp-name">{name}</span>
            {last != null && <span className={`mp-last ${cls}`}>{last.toFixed(2)}</span>}
            {chg != null && <span className={`mp-chg ${cls}`}>{chg >= 0 ? "+" : ""}{chg.toFixed(2)}</span>}
            {pct != null && <span className={`mp-pct ${cls}`}>{fmtPct(pct)}</span>}
          </span>
        </div>

        {/* 顶栏 2：代码 + 周期 + 复权 + 指标 + 根数 + 刷新 */}
        <div className="mp-toolbar">
          <input className="mp-code-input" value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && subscribe()}
            placeholder="代码 如 600519.SH" />
          <button onClick={subscribe}>订阅</button>

          <div className="mp-periods">
            {periodList.map((p) => {
              const off = p.supported === false;
              return (
                <button key={p.v}
                  className={`mp-pd ${period === p.v ? "active" : ""}${off ? " disabled" : ""}`}
                  disabled={off}
                  onClick={() => !off && setPeriod(p.v)}
                  title={off ? (p.reason || "该周期暂不可用") : p.label}>{p.label}</button>
              );
            })}
          </div>

          <select value={adj} onChange={(e) => setAdj(e.target.value)} title="复权">
            {ADJ_TYPES.map((a) => <option key={a.v} value={a.v}>{a.label}</option>)}
          </select>

          <label className="muted">根数</label>
          <input type="number" className="mp-count-input" min={30} max={1000} value={count}
            onChange={(e) => setCount(Math.max(30, Math.min(1000, +e.target.value || 180)))} />

          <select value={mainInd} onChange={(e) => setMainInd(e.target.value)} title="主图指标">
            {MAIN_INDICATORS.map((ind) => <option key={ind.v} value={ind.v}>{ind.label}</option>)}
          </select>
          <select value={subInd} onChange={(e) => setSubInd(e.target.value)} title="副图指标">
            {SUB_INDICATORS.map((ind) => <option key={ind.v} value={ind.v}>{ind.label}</option>)}
          </select>

          <button className="ghost btn-sm" onClick={forceKline} disabled={loading}>
            {loading ? "加载…" : "刷新"}
          </button>
        </div>

        <div className="mp-status">
          <span>{periodMeta.label}</span>
          {periodMeta.kind === "tick"
            ? (<> · 分时 {minutesData?.points?.length || 0} 点
                {minutesData?.trading_date ? ` · ${minutesData.trading_date}` : ""}
                {minutesErr ? <span className="down"> · {minutesErr}</span> : null}</>)
            : <span> · 共 {bars.length} 根</span>}
          {meta && (<> · 数据来源 <span className={`tag ok src-${meta.source}`}>{SRC_LABEL[meta.source] || meta.source}</span></>)}
        </div>

        {/* 多维摘要条（I1）：换手/量比/净流入 + 行业概念深链板块页（F3 联动） */}
        <div className="mp-brief">
          <span>换手 <b>{turnover != null ? turnover.toFixed(2) + "%" : "—"}</b></span>
          <span>量比 <b>{mf?.volume_ratio != null ? mf.volume_ratio : "—"}</b></span>
          <span className={mfNet == null ? "" : mfNet >= 0 ? "up" : "down"}>
            净流入 <b>{mfNet != null ? mfNet.toLocaleString() + " 手" : "—"}</b></span>
          {industryName && (
            <button className="qp-concept-tag" title="打开板块行情"
              onClick={() => toBoards(industryName)}>{industryName}</button>
          )}
          {conceptList.slice(0, 3).map((c) => (
            <button key={c} className="qp-concept-tag" title="打开板块行情"
              onClick={() => toBoards(c)}>{c}</button>
          ))}
        </div>
      </div>

      {err && <div className="toast err">{err}</div>}

      {/* 主体：图表全宽（去掉旧版右侧 280px 固定栏） + 底部可折叠抽屉 */}
      <div className="mp-body mp-body-v3">
        <div className="mp-chart-area">
          <div className="card mp-chart-card">
            {periodMeta.kind !== "tick" && (
              <div className="kline-ohlc-bar">
                {ohlc ? (
                  <>
                    <span className="ob-date">{ohlc.date}</span>
                    <span className={`ob-item ${ohlc.open >= ohlc.prev ? "up" : "down"}`}>开 <b>{ohlc.open.toFixed(2)}</b></span>
                    <span className={`ob-item ${ohlc.high >= ohlc.prev ? "up" : "down"}`}>高 <b>{ohlc.high.toFixed(2)}</b></span>
                    <span className={`ob-item ${ohlc.low >= ohlc.prev ? "up" : "down"}`}>低 <b>{ohlc.low.toFixed(2)}</b></span>
                    <span className={`ob-item ${ohlc.close >= ohlc.prev ? "up" : "down"}`}>收 <b>{ohlc.close.toFixed(2)}</b></span>
                    <span className={`ob-item ob-pct ${ohlc.pct >= 0 ? "up" : "down"}`}>{ohlc.pct >= 0 ? "+" : ""}{ohlc.pct.toFixed(2)}%</span>
                  </>
                ) : (
                  <span className="muted">—</span>
                )}
              </div>
            )}
            <ErrorBoundary>
              <Chart ref={chartRef} option={periodMeta.kind === "tick" ? tickOption : klineOption} height={DRAWER_CHART_H[drawerOpen ? "open" : "closed"]} />
            </ErrorBoundary>
          </div>
          {periodMeta.kind !== "tick" && (
            <div className="kline-draw-bar">
              <button className={`ib-btn ${drawMode ? "active" : ""}`}
                onClick={() => setDrawMode((v) => !v)} title="开启后点击K线画水平线">✎ 画线</button>
              {drawings.length > 0 && (
                <button className="btn-danger-sm" onClick={clearDrawings}>清除画线 ({drawings.length})</button>
              )}
            </div>
          )}

          <div className="indicator-bar">
            {SUB_INDICATORS.map((ind) => (
              <button key={ind.v} className={`ib-btn ${subInd === ind.v ? "active" : ""}`}
                onClick={() => setSubInd(ind.v)}>{ind.label}</button>
            ))}
          </div>
        </div>

        {/* 底部抽屉：五档盘口 / 快速交易 / F10 —— 可折叠，图表随高度自适应 */}
        <div className={`mp-drawer ${drawerOpen ? "open" : ""}`}>
          <div className="mp-drawer-bar">
            <div className="mp-drawer-tabs">
              <button className={`mp-drawer-tab ${drawerTab === "quote" ? "active" : ""}`}
                onClick={() => { setDrawerTab("quote"); setDrawerOpen(true); }}>五档盘口</button>
              <button className={`mp-drawer-tab ${drawerTab === "trade" ? "active" : ""}`}
                onClick={() => { setDrawerTab("trade"); setDrawerOpen(true); }}>快速交易</button>
              <button className={`mp-drawer-tab ${drawerTab === "f10" ? "active" : ""}`}
                onClick={() => { setDrawerTab("f10"); setDrawerOpen(true); }}>F10 财务</button>
              <button className={`mp-drawer-tab ${drawerTab === "mf" ? "active" : ""}`}
                onClick={() => { setDrawerTab("mf"); setDrawerOpen(true); }}>多维摘要</button>
              <button className={`mp-drawer-tab ${drawerTab === "link" ? "active" : ""}`}
                onClick={() => { setDrawerTab("link"); setDrawerOpen(true); }}>同板块联动</button>
            </div>
            <button className="mp-drawer-toggle" title={drawerOpen ? "收起" : "展开"}
              onClick={() => setDrawerOpen((v) => !v)}>{drawerOpen ? "▾ 收起" : "▴ 展开"}</button>
          </div>
          {drawerOpen && (
            <div className="mp-drawer-body">
              {drawerTab === "quote" && (
                <div className="mp-drawer-panel">
                  <QuotePanel tick={tick} stockInfo={stockInfo} code={code} bars={bars} />
                </div>
              )}
              {drawerTab === "trade" && (
                <div className="mp-drawer-panel">
                  <div className="card qt-card">
                    <div className="qt-side">
                      <button className={`qt-side-btn buy ${qtDir === "buy" ? "active" : ""}`}
                        onClick={() => setQtDir("buy")}>买</button>
                      <button className={`qt-side-btn sell ${qtDir === "sell" ? "active" : ""}`}
                        onClick={() => setQtDir("sell")}>卖</button>
                    </div>
                    <div className="qt-row">
                      <label>价格</label>
                      <select value={qtPriceKind} onChange={(e) => setQtPriceKind(e.target.value)}>
                        {PRICE_TICKS.map((t) => <option key={t.v} value={t.v}>{t.label}</option>)}
                      </select>
                    </div>
                    <div className="qt-row">
                      <label>委托价</label>
                      <input type="number" step="0.01" value={qtPrice}
                        onChange={(e) => setQtPrice(e.target.value)} />
                    </div>
                    <div className="qt-row">
                      <label>数量</label>
                      <input type="number" step="100" value={qtVol}
                        onChange={(e) => setQtVol(Math.max(0, +e.target.value || 0))} />
                    </div>
                    <div className="qt-actions">
                      <button className={`qt-submit ${qtDir}`} disabled={qtSending || !activeId}
                        onClick={submitOrder}>
                        {qtSending ? "提交中…" : (qtDir === "buy" ? "买入" : "卖出")}
                      </button>
                      <button className="ghost btn-sm"
                        onClick={() => quickTradeNavigate(code, Number(qtPrice) || last, qtDir)}>
                        委托面板
                      </button>
                    </div>
                    {qtMsg && <div className={`qt-msg ${qtMsgType || "err"}`}>{qtMsg}</div>}
                    {!activeId && <div className="muted qt-hint">未连接券商：可编辑但无法提交</div>}
                  </div>
                </div>
              )}
              {drawerTab === "f10" && (
                <div className="mp-drawer-panel">
                  <div className="card sa-f10 mp-drawer-f10">
                    {(financial || stockInfo?.main_business) ? (
                      <>
                        {stockInfo?.main_business && (
                          <div className="sa-f10-row"><span>主营</span><b>{stockInfo.main_business}</b></div>
                        )}
                        {financial?.pe != null && (
                          <div className="sa-f10-row"><span>PE(TTM)</span><b>{Number(financial.pe).toFixed(2)}</b></div>
                        )}
                        {financial?.pb != null && (
                          <div className="sa-f10-row"><span>PB</span><b>{Number(financial.pb).toFixed(2)}</b></div>
                        )}
                        {financial?.roe != null && (
                          <div className="sa-f10-row"><span>ROE</span><b>{(Number(financial.roe) * 100).toFixed(2) + "%"}</b></div>
                        )}
                        {financial?.revenue != null && (
                          <div className="sa-f10-row"><span>营收</span><b>{fmtAmount(financial.revenue)}</b></div>
                        )}
                        {financial?.net_profit != null && (
                          <div className="sa-f10-row"><span>净利</span><b>{fmtAmount(financial.net_profit)}</b></div>
                        )}
                        {financial?.market_cap != null && (
                          <div className="sa-f10-row"><span>总市值</span><b>{fmtAmount(financial.market_cap)}</b></div>
                        )}
                      </>
                    ) : (
                      <div className="muted">暂无财务数据（连接券商或等待快照后重试）</div>
                    )}
                  </div>
                </div>
              )}
              {drawerTab === "mf" && (
                <div className="mp-drawer-panel">
                  <div className="card mf-card">
                    <div className="mf-head">
                      <span className="mf-title">资金 / 股本 / 板块（真实口径）</span>
                      {mf?.est === false && <span className="tag ok">真实口径</span>}
                      {mf?.est === true && <span className="tag fail">估算口径</span>}
                      <button className="ghost btn-sm" onClick={refreshMf}>刷新</button>
                    </div>
                    {mfErr && <div className="down mf-note">{mfErr}</div>}
                    <div className="mf-grid">
                      <div className="mf-row"><span>净流入</span>
                        <b className={mfNet == null ? "" : mfNet >= 0 ? "up" : "down"}>
                          {mfNet != null ? mfNet.toLocaleString() + " 手" : "—"}</b></div>
                      <div className="mf-row"><span>内盘</span>
                        <b>{mfInside != null ? mfInside.toLocaleString() + " 手" : "—"}</b></div>
                      <div className="mf-row"><span>外盘</span>
                        <b>{mfOutside != null ? mfOutside.toLocaleString() + " 手" : "—"}</b></div>
                      <div className="mf-row"><span>量比</span>
                        <b>{mf?.volume_ratio != null ? mf.volume_ratio : "—"}</b></div>
                      <div className="mf-row"><span>换手率</span>
                        <b>{turnover != null ? turnover.toFixed(2) + "%" : "—"}</b></div>
                      <div className="mf-row"><span>流通股本</span>
                        <b>{circShares != null ? (circShares / 1e8).toFixed(2) + " 亿股" : "—"}</b></div>
                      <div className="mf-row"><span>涨停</span>
                        <b className="up">{limitInfo?.limit_up != null ? Number(limitInfo.limit_up).toFixed(2) : "—"}</b></div>
                      <div className="mf-row"><span>跌停</span>
                        <b className="down">{limitInfo?.limit_down != null ? Number(limitInfo.limit_down).toFixed(2) : "—"}</b></div>
                      <div className="mf-row"><span>行业</span><b>{industryName || "—"}</b></div>
                    </div>
                    {conceptList.length > 0 && (
                      <div className="mf-concepts">
                        {conceptList.slice(0, 8).map((c) => (
                          <button key={c} className="qp-concept-tag" title="打开板块行情"
                            onClick={() => toBoards(c)}>{c}</button>
                        ))}
                      </div>
                    )}
                    {Array.isArray(mf?.strength) && mf.strength.length > 0 && (
                      <div className="mf-strength">
                        <div className="mf-sub muted">买卖力道（分钟级主买/主卖委托，最近 8 分钟）</div>
                        <table className="bd-table">
                          <thead><tr><th>时间</th><th className="num">主买</th><th className="num">主卖</th></tr></thead>
                          <tbody>
                            {mf.strength.slice(-8).reverse().map((p, i) => (
                              <tr key={`${p.t || ""}-${i}`}>
                                <td>{p.t || "—"}</td>
                                <td className="num up">{p.buy != null ? Number(p.buy).toLocaleString() : "—"}</td>
                                <td className="num down">{p.sell != null ? Number(p.sell).toLocaleString() : "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    {/* C2：资金流日内回放（G3 观测池真实落库序列） */}
                    <div className="mf-replay">
                      <div className="mf-sub muted">资金流日内回放（自动采集快照 · 交易时段每 5 分钟）</div>
                      {replayErr && <div className="down">{replayErr}</div>}
                      {!replayErr && replayOption
                        ? <Chart option={replayOption} height={150} />
                        : !replayErr && <div className="muted">暂无快照（采集运行一段时间后此处显示净流入曲线）</div>}
                    </div>
                  </div>
                </div>
              )}
              {drawerTab === "link" && (
                <div className="mp-drawer-panel">
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
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// 抽屉展开/收起时图表高度自适应（收起更高，给 K 线更多空间）
const DRAWER_CHART_H = { open: 460, closed: 640 };
