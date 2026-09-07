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
// - 结构：2026-09 拆分为 components/marketdata/*（常量/option 构建器/子面板）+ hooks，
//         本文件只保留编排与图表事件接线（行为零变更）。
import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";
import QuotePanel from "./QuotePanel.jsx";
import { navToQuote } from "../lib/nav.js";
import { useKline, useFundamentals, formatPct, preCloseOf, applyTickToBars } from "../hooks/useMarket.js";
import { useQuotes } from "../lib/quoteHub.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
import { useChartSpec, mainIndicatorOptions, subIndicatorOptions, broadcastIndicatorChange } from "../lib/chartConfig.jsx";
import { badgeOf } from "../lib/instrument.js";

// G2-3：指标单一真源——前端不再自带指标计算，统一消费后端引擎
import { fetchIndicator, indicatorKey } from "../lib/indicators.js";
// 金额格式化直接引唯一实现（不再经 useMarket 别名中转，链路更短更明确）
import { fmtTs } from "../lib/format.js";
import { t as _t } from "../lib/i18n.js";

import {
  PERIODS, ADJ_TYPES, MAIN_INDICATORS, SUB_INDICATORS,
  SRC_LABEL, DRAWER_CHART_H, ANALYSIS_TTL, analysisCache,
} from "./marketdata/constants.js";
import { buildTickOption, buildKlineOption } from "./marketdata/options.js";
import { toBoards } from "./marketdata/boards.js";
import QuickTrade from "./marketdata/QuickTrade.jsx";
import F10Panel from "./marketdata/F10Panel.jsx";
import MfPanel from "./marketdata/MfPanel.jsx";
import LinkPanel from "./marketdata/LinkPanel.jsx";
import { useMfSummary } from "../hooks/useMfSummary.js";
import { useBoardLink } from "../hooks/useBoardLink.js";
import { on as onEvent, off as offEvent } from "../lib/eventBus";

// 旧版遗留的悬空引用（fmtPct 未定义，涨跌幅一渲染就会 ReferenceError）——根治为别名
const fmtPct = formatPct;

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
  const { spec: chartSpec } = useChartSpec();   // G9 chart-spec 单一真源
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

  // 底部抽屉：五档盘口 | 快速交易 | F10 | 多维摘要 | 同板块联动
  const [drawerTab, setDrawerTab] = useState("quote");
  const [drawerOpen, setDrawerOpen] = useState(true);
  // 快速交易表单重置信号：订阅/外部换票时递增（保持拆分前行为：表单价格与消息清空，
  // 价格档随后由 tick 自动跟随重填）
  const [qtReset, setQtReset] = useState(0);

  // 外部导航换票（F3/F4/报价牌/命令面板 → replace 参数）：就地切换，不丢分屏与指标状态
  useEffect(() => {
    if (!paramCode || paramCode === code) return;
    setCode(paramCode);
    setInput(paramCode);
    setTick(null);
    setBarsOverlay([]);
    setQtReset((n) => n + 1);
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

  /* ---------- 标的画像条（检索分析体系③）：/market/analysis 六维聚合 ----------
   * 单请求拿全 快照/画像/股本/表现/资金流/估值；任一维度缺失显「—」
   * （availability 标注），模块级缓存 30s TTL 切回秒出，不重复打后端。 */
  const [analysis, setAnalysis] = useState(null);
  useEffect(() => {
    if (!code) return;
    const cached = analysisCache.get(code);
    if (cached && Date.now() - cached.ts < ANALYSIS_TTL) { setAnalysis(cached.data); return; }
    let alive = true;
    setAnalysis(null);
    api.marketAnalysis({ code })
      .then((r) => {
        if (!alive) return;
        analysisCache.set(code, { ts: Date.now(), data: r });
        setAnalysis(r);
      })
      .catch(() => { if (alive) setAnalysis(null); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

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
      [5, 10, 20, 60].forEach((p) => need.push(["ma", { win: p }]));
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
  const chartRef = useRef(null);
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
    as_of: klineQ.data.as_of,
    stale: klineQ.data.stale,
    note: klineQ.data.note,
    count: klineQ.data.count,
  } : null;
  const loading = klineQ.loading;
  const err = klineQ.err;

  // C1：十字光标 OHLC 信息条 —— 引用最新 bars / 是否 K 线，供图表事件回调读取
  const [ohlc, setOhlc] = useState(null); // { date, open, high, low, close, prev, pct }
  const barsRef = useRef(bars); barsRef.current = bars;
  const isKlineRef = useRef(true); isKlineRef.current = periodMeta.kind !== "tick";

  // stockInfo / financial 从 hook 解构
  const stockInfo = fundQ.info;
  const financial = fundQ.fin;

  /* ---------- 多维摘要（I1）：资金流 + 股本/涨跌停（hooks/useMfSummary.js） ---------- */
  const { mf, mfErr, cap, refreshMf, mfRefresh } = useMfSummary(code);

  /* ---------- 同板块联动（I2）：hooks/useBoardLink.js ---------- */
  const industryName = stockInfo?.industry || null;
  const { linkName, linkCons, linkLoading, linkQuotes, loadLink, resetLink } = useBoardLink(code, industryName);
  // 切换股票/行业时自动联动到所属行业板块
  useEffect(() => {
    if (industryName) loadLink(industryName);
    else resetLink();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, industryName]);

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

  const hubQuote = quotes[code];
  useEffect(() => {
    if (!hubQuote) return;
    setTick(hubQuote);
    setBarsOverlay((prev) => applyTickToBars(prev.length ? prev : baseBars, hubQuote));
  }, [hubQuote, baseBars]);

  /* ---------- F5 切分时↔K线 ---------- */
  useEffect(() => {
    const onToggle = () => setPeriod((p) => (p === "tick" ? "1d" : "tick"));
    onEvent("sa:toggle-period", onToggle);
    return () => offEvent("sa:toggle-period", onToggle);
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
    onEvent("sa:focus-f10", onFocus);
    return () => offEvent("sa:focus-f10", onFocus);
  }, []);

  /* ---------- 派生：TDX 同款色块（红涨绿跌） ---------- */
  const last = tick?.last != null ? Number(tick.last) : null;
  const pre = preCloseOf(tick, stockInfo);
  const pct = last != null && pre ? ((last - pre) / pre) * 100 : null;
  const chg = last != null && pre ? last - pre : null;
  const cls = pct == null ? "" : pct >= 0 ? "up" : "down";
  const name = stockInfo?.name || tick?.name || code;

  /* ---------- 派生：多维摘要口径（真实数据，缺失显「—」） ---------- */
  const circShares = cap?.shares?.[code]?.circulating_shares != null
    ? Number(cap.shares[code].circulating_shares) : null;
  // 换手率 = 成交量(手)×100 / 流通股本(股) × 100%
  const turnover = tick?.volume != null && circShares
    ? (Number(tick.volume) * 100 / circShares) * 100 : null;
  const mfNet = mf?.net != null ? Number(mf.net) : null;
  const mfInside = mf?.inside != null ? Number(mf.inside) : null;
  const mfOutside = mf?.outside != null ? Number(mf.outside) : null;
  const conceptList = Array.isArray(stockInfo?.concepts) ? stockInfo.concepts : [];

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
    setQtReset((n) => n + 1);
    // 回写叶子参数：tab 标题跟随代码，刷新/重开也能恢复
    if (dispatch && tabId && leafId) {
      dispatch({ type: "LEAF_PARAMS", tabId, leafId, params: { code: sym } });
    }
  }, [input, dispatch, tabId, leafId]);

  /* ======================== 图表 option（纯函数构建，见 marketdata/options.js） ======================== */
  const tickOption = useMemo(
    () => buildTickOption({ minutesData, minutesErr, tick, stockInfo, preCloseOf }),
    [minutesData, minutesErr, tick, stockInfo]);

  const klineOption = useMemo(
    () => buildKlineOption({ bars, mainInd, subInd, err, indData, drawings }),
    [bars, mainInd, subInd, err, indData, drawings]);

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
      setOhlc({ date: (b.time || b.date || "").slice(0, 10), open, high, low, close, prev, pct: barPct });
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
          <h2 className="page-title">{_t(`page.quote.title`)}</h2>
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

        {/* 标的画像条（检索分析体系③）：类型徽章 + 近期表现 + 估值摘要；缺失维度显「—」 */}
        <div className="mp-profile">
          <span className={`gbadge ${badgeOf(analysis?.type).cls}`}>{badgeOf(analysis?.type).label}</span>
          <span className="mp-pf-name">{name}</span>
          <span className="muted">{analysis?.exchange || "—"} · {analysis?.board || "—"}</span>
          <span className="mp-pf-sep" />
          {[["5日", "chg_5d"], ["20日", "chg_20d"], ["60日", "chg_60d"]].map(([lb, k]) => {
            const v = analysis?.performance?.[k];
            return (
              <span key={k} className={v == null ? "muted" : v >= 0 ? "up" : "down"}>
                {lb} {v != null ? (v >= 0 ? "+" : "") + v.toFixed(2) + "%" : "—"}</span>
            );
          })}
          <span>52周分位 <b className="mp-pf-plain">{analysis?.performance?.pct_in_52w != null ? analysis.performance.pct_in_52w.toFixed(0) + "%" : "—"}</b></span>
          {analysis?.performance?.stale && (
            <span className="mp-pf-stale" title="券商本地 K 线未同步且公共源补数失败，表现数据截至以下日期">
              表现截至 {analysis.performance.as_of || "旧缓存"}
            </span>
          )}
          <span>换手 <b className="mp-pf-plain">{analysis?.capital?.turnover_rate != null ? analysis.capital.turnover_rate.toFixed(2) + "%" : "—"}</b></span>
          <span className={analysis?.moneyflow?.net == null ? "muted" : analysis.moneyflow.net >= 0 ? "up" : "down"}>
            净流入 {analysis?.moneyflow?.net != null ? analysis.moneyflow.net.toLocaleString() + " 手" : "—"}</span>
          {(analysis?.valuation?.pe != null || analysis?.valuation?.pb != null)
            ? <span>PE <b className="mp-pf-plain">{analysis.valuation.pe != null ? analysis.valuation.pe : "—"}</b>
                · PB <b className="mp-pf-plain">{analysis.valuation.pb != null ? analysis.valuation.pb : "—"}</b></span>
            : <span className="muted">{activeId ? "估值 —" : "估值需券商连接"}</span>}
          {analysis?.snapshot?.source && (
            <span className="muted">数据源: {SRC_LABEL[analysis.snapshot.source] || analysis.snapshot.source}</span>
          )}
        </div>

        {/* 顶栏 2：代码 + 周期 + 复权 + 指标 + 根数 + 刷新 */}
        <div className="mp-toolbar">
          <input className="mp-code-input" value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && subscribe()}
            placeholder="代码 如 600519.SH"
            maxLength={20} aria-label="股票代码" />
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

          <select value={mainInd}
            onChange={(e) => { setMainInd(e.target.value); broadcastIndicatorChange(e.target.value, subInd); }}
            title="主图指标">
            {(mainIndicatorOptions(chartSpec).length > 1 ? mainIndicatorOptions(chartSpec) : MAIN_INDICATORS)
              .map((ind) => <option key={ind.v} value={ind.v}>{ind.label}</option>)}
          </select>
          <select value={subInd}
            onChange={(e) => { setSubInd(e.target.value); broadcastIndicatorChange(mainInd, e.target.value); }}
            title="副图指标">
            {(subIndicatorOptions(chartSpec).length > 0 ? subIndicatorOptions(chartSpec) : SUB_INDICATORS)
              .map((ind) => <option key={ind.v} value={ind.v}>{ind.label}</option>)}
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
          {meta?.stale && (
            <span className="tag warn" title={meta.note || "本地缓存已过期，未取得最新行情"}>
              数据已过期{meta.as_of ? ` · 截至 ${fmtTs(meta.as_of)}` : ""}
            </span>
          )}
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

        {/* 底部抽屉：五档盘口 / 快速交易 / F10 / 多维摘要 / 同板块联动 —— 可折叠，图表随高度自适应 */}
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
                  <QuickTrade code={code} tick={tick} stockInfo={stockInfo}
                    last={last} pre={pre} activeId={activeId} resetSignal={qtReset} />
                </div>
              )}
              {drawerTab === "f10" && (
                <div className="mp-drawer-panel">
                  <F10Panel financial={financial} stockInfo={stockInfo} />
                </div>
              )}
              {drawerTab === "mf" && (
                <div className="mp-drawer-panel">
                  <MfPanel code={code} mf={mf} mfErr={mfErr} cap={cap}
                    turnover={turnover} industryName={industryName} conceptList={conceptList}
                    mfNet={mfNet} mfInside={mfInside} mfOutside={mfOutside}
                    refreshToken={mfRefresh} refreshMf={refreshMf} />
                </div>
              )}
              {drawerTab === "link" && (
                <div className="mp-drawer-panel">
                  <LinkPanel code={code} linkName={linkName} linkCons={linkCons}
                    linkLoading={linkLoading} linkQuotes={linkQuotes}
                    industryName={industryName} conceptList={conceptList} loadLink={loadLink} />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
