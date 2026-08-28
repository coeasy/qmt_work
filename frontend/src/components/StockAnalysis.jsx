// 个股分析页（通达信式）：K线 + 五档 + 分时 + F10，支持实时刷新。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import Chart from "./Chart.jsx";
import QuotePanel from "./QuotePanel.jsx";
import { useBroker } from "../BrokerContext.jsx";

const PERIODS = [
  { v: "tick", label: "分时", kind: "tick" },
  { v: "1m", label: "1分", kind: "minute" },
  { v: "5m", label: "5分", kind: "minute" },
  { v: "15m", label: "15分", kind: "minute" },
  { v: "30m", label: "30分", kind: "minute" },
  { v: "60m", label: "60分", kind: "minute" },
  { v: "1d", label: "日线", kind: "kline" },
  { v: "1w", label: "周线", kind: "kline" },
  { v: "1mo", label: "月线", kind: "kline" },
];
const COUNT = 180;
const MA = [5, 10, 20, 60];

function calcMA(data, period) {
  const r = [];
  let sum = 0;
  for (let i = 0; i < data.length; i++) {
    sum += data[i][1]; // close
    if (i >= period) sum -= data[i - period][1];
    r.push(i < period - 1 ? null : sum / period);
  }
  return r;
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

export default function StockAnalysis() {
  const { activeId } = useBroker();
  const [code, setCode] = useState(() => {
    try { return sessionStorage.getItem("qmt_work.code") || "600519.SH"; } catch { return "600519.SH"; }
  });
  const [input, setInput] = useState(code);
  const [period, setPeriod] = useState("1d");
  const [bars, setBars] = useState([]);
  const [tick, setTick] = useState(null);
  const [stockInfo, setStockInfo] = useState(null);
  const [financial, setFinancial] = useState(null);
  const [loading, setLoading] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => { setInput(code); }, [code]);

  // F5：分时 ↔ K线 切换
  useEffect(() => {
    const onToggle = () => {
      setPeriod((p) => (p === "tick" ? "1d" : "tick"));
    };
    window.addEventListener("sa:toggle-period", onToggle);
    return () => window.removeEventListener("sa:toggle-period", onToggle);
  }, []);

  // F10：聚焦 F10 资料块（scrollIntoView + 高亮闪烁）
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

  // 切换代码：拉快照 + K线 + F10（period 改变时也重拉）
  useEffect(() => {
    if (!code) return;
    let canceled = false;
    setLoading(true);
    // 拉取时跳过 "tick"（分时由实时 tick 渲染，不走 kline 接口），用 1d 取昨收 + 历史 K 线兜底
    const fetchPeriod = period === "tick" ? "1d" : period;
    (async () => {
      try {
        const [q, k, info, fin] = await Promise.all([
          api.marketQuote({ code, conn_id: activeId }).catch(() => null),
          api.marketKline({ code, period: fetchPeriod, count: COUNT, conn_id: activeId }).catch(() => ({ bars: [] })),
          api.marketStockInfo({ code, conn_id: activeId }).catch(() => null),
          api.financial(code).catch(() => null),
        ]);
        if (canceled) return;
        // 实时 tick 来自独立 WS 通道（见下）；初始快照先填 tick 保证分时能立即渲染
        setTick((prev) => prev || q);
        setBars(k.bars || []);
        setStockInfo(info);
        setFinancial(fin);
      } finally { if (!canceled) setLoading(false); }
    })();
    return () => { canceled = true; };
  }, [code, period, activeId]);

  // 实时 tick
  useEffect(() => {
    if (!code) return;
    let stopped = false;
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => { try { ws.send(JSON.stringify({ action: "subscribe", codes: [code] })); } catch {} };
    ws.onmessage = (e) => {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      const d = msg.data;
      if (msg.type === "quote" && d && d.code === code) setTick(d);
      else if (msg.type === "quotes" && Array.isArray(d?.items)) {
        const it = d.items.find((x) => x.code === code); if (it) setTick(it);
      }
    };
    ws.onerror = () => { try { ws.close(); } catch {} };
    return () => { stopped = true; try { ws.close(); } catch {} wsRef.current = null; };
  }, [code]);

  const periodMeta = PERIODS.find((p) => p.v === period) || PERIODS[6];

  const option = useMemo(() => {
    // 分时图：基于实时 tick + 昨收参考线
    if (periodMeta.kind === "tick") {
      const preClose = tick?.preClose != null ? Number(tick.preClose) : null;
      if (!preClose) {
        return { title: { text: "分时需昨收价（连接券商后显示）", left: "center", top: "center", textStyle: { color: "#8aa0c0" } } };
      }
      const last = tick?.last != null ? Number(tick.last) : preClose;
      const open = tick?.open != null ? Number(tick.open) : preClose;
      const high = tick?.high != null ? Number(tick.high) : Math.max(open, last);
      const low = tick?.low != null ? Number(tick.low) : Math.min(open, last);
      const vol = tick?.volume != null ? Number(tick.volume) : 0;
      const data = [open, last];
      const lineSeries = [{
        name: "分时", type: "line", data, smooth: true, showSymbol: false,
        lineStyle: { width: 1.5, color: last >= preClose ? "#ff4d4f" : "#19c37d" },
        areaStyle: { color: last >= preClose ? "rgba(255,77,79,.18)" : "rgba(25,195,125,.18)" },
        markLine: {
          symbol: "none", silent: true,
          lineStyle: { color: "#8aa0c0", type: "dashed" },
          data: [{ yAxis: preClose, label: { formatter: "昨收 " + preClose.toFixed(2), color: "#8aa0c0" } }],
        },
      }];
      const yMin = Math.min(low, preClose) * 0.999;
      const yMax = Math.max(high, preClose) * 1.001;
      return {
        animation: false,
        grid: [{ left: 48, right: 16, top: 16, height: "62%" }, { left: 48, right: 16, top: "72%", height: "20%" }],
        xAxis: [
          { type: "category", data: ["开盘", "现"], axisLine: { lineStyle: { color: "#3a4a66" } }, axisLabel: { color: "#8aa0c0" } },
          { type: "category", gridIndex: 1, data: ["现"], axisLabel: { show: false }, axisLine: { lineStyle: { color: "#3a4a66" } } },
        ],
        yAxis: [
          { scale: true, min: yMin, max: yMax, splitLine: { lineStyle: { color: "rgba(58,74,102,.3)" } }, axisLabel: { color: "#8aa0c0" } },
          { gridIndex: 1, splitLine: { show: false }, axisLabel: { color: "#8aa0c0" } },
        ],
        tooltip: { trigger: "axis" },
        series: [
          ...lineSeries,
          { name: "现量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: [vol],
            itemStyle: { color: last >= preClose ? "#ff4d4f" : "#19c37d" } },
        ],
      };
    }

    if (!bars.length) return { title: { text: "无K线数据", left: "center", top: "center", textStyle: { color: "#8aa0c0" } } };
    const dates = bars.map((b) => b.time || b.date);
    const ohlc = bars.map((b) => [+b.open, +b.close, +b.low, +b.high]);
    const vols = bars.map((b, i) => [i, +b.volume, +b.close >= +b.open ? 1 : -1]);
    const maSeries = MA.map((p) => ({
      name: `MA${p}`, type: "line", data: calcMA(bars, p),
      smooth: true, showSymbol: false, lineStyle: { width: 1 },
    }));
    return {
      animation: false,
      grid: [{ left: 48, right: 16, top: 16, height: "62%" }, { left: 48, right: 16, top: "72%", height: "20%" }],
      xAxis: [
        { type: "category", data: dates, axisLine: { lineStyle: { color: "#3a4a66" } }, axisLabel: { color: "#8aa0c0" } },
        { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#3a4a66" } } },
      ],
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: "rgba(58,74,102,.3)" } }, axisLabel: { color: "#8aa0c0" } },
        { gridIndex: 1, splitLine: { show: false }, axisLabel: { color: "#8aa0c0" } },
      ],
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: { data: ["K线", ...MA.map((p) => `MA${p}`)], textStyle: { color: "#8aa0c0" }, top: 0 },
      series: [
        { name: "K线", type: "candlestick", data: ohlc,
          itemStyle: { color: "#ff4d4f", color0: "#19c37d", borderColor: "#ff4d4f", borderColor0: "#19c37d" } },
        ...maSeries,
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols.map((v) => ({ value: v[1], itemStyle: { color: v[2] > 0 ? "#ff4d4f" : "#19c37d" } })) },
      ],
    };
  }, [bars, periodMeta, tick]);

  const go = () => {
    const c = input.trim().toUpperCase();
    if (!c) return;
    try { sessionStorage.setItem("qmt_work.code", c); } catch {}
    setCode(c);
  };

  const last = tick?.last != null ? Number(tick.last) : (tick?.price != null ? Number(tick.price) : null);
  const pre = tick?.preClose != null ? Number(tick.preClose) : (stockInfo?.preClose != null ? Number(stockInfo.preClose) : null);
  const pct = last != null && pre ? ((last - pre) / pre) * 100 : null;
  const chg = last != null && pre ? last - pre : null;
  const cls = pct == null ? "" : pct >= 0 ? "up" : "down";

  return (
    <div className="page stock-analysis">
      <div className="sa-toolbar">
        <input className="sa-input" value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && go()} placeholder="输入代码，如 600519.SH" />
        <button className="btn-sm" onClick={go}>分析</button>
        <select className="sa-period" value={period} onChange={(e) => setPeriod(e.target.value)}>
          {PERIODS.map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
        </select>
        <span className="sa-name">{stockInfo?.name || tick?.name || code}</span>
        {last != null && <span className={`sa-price ${cls}`}>{last.toFixed(2)}</span>}
        {pct != null && <span className={`sa-chg ${cls}`}>{(chg >= 0 ? "+" : "") + chg.toFixed(2)} ({(pct >= 0 ? "+" : "") + pct.toFixed(2)}%)</span>}
        {loading && <span className="muted">加载中…</span>}
      </div>

      <div className="sa-body split-2">
        <div className="sa-chart">
          <Chart option={option} height={460} />
        </div>
        <div className="sa-side">
          <QuotePanel tick={tick} stockInfo={stockInfo} code={code} bars={bars} />
          <div className="sa-f10">
            <div className="sa-f10-title">F10 概况</div>
            <div className="sa-f10-row"><span>行业</span><b>{stockInfo?.industry || "—"}</b></div>
            <div className="sa-f10-row"><span>概念</span><b>{(stockInfo?.concepts || []).join("、") || "—"}</b></div>
            <div className="sa-f10-row"><span>主营</span><b>{stockInfo?.main_business || "—"}</b></div>
            {financial && (
              <>
                <div className="sa-f10-row"><span>总市值</span><b>{financial.market_cap ? (financial.market_cap / 1e8).toFixed(2) + "亿" : "—"}</b></div>
                <div className="sa-f10-row"><span>PE(TTM)</span><b>{financial.pe != null ? financial.pe.toFixed(2) : "—"}</b></div>
                <div className="sa-f10-row"><span>ROE</span><b>{financial.roe != null ? (financial.roe * 100).toFixed(2) + "%" : "—"}</b></div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
