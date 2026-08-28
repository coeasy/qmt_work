import { useEffect, useRef, useState, useMemo, useCallback } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";
import QuotePanel from "./QuotePanel.jsx";
import { quickTradeNavigate } from "../lib/trade.js";

/* ======================== 常量 ======================== */
const PERIODS = [
  { v: "1m", label: "1\u5206\u949f" },
  { v: "5m", label: "5\u5206\u949f" },
  { v: "15m", label: "15\u5206\u949f" },
  { v: "30m", label: "30\u5206\u949f" },
  { v: "60m", label: "60\u5206\u949f" },
  { v: "1d", label: "\u65e5\u7ebf" },
  { v: "week", label: "\u5468\u7ebf" },
  { v: "month", label: "\u6708\u7ebf" },
];

const ADJ_TYPES = [
  { v: "", label: "\u4e0d\u590d\u6743" },
  { v: "qfq", label: "\u524d\u590d\u6743" },
  { v: "hfq", label: "\u540e\u590d\u6743" },
];

/* \u4e3b\u56fe\u53e0\u52a0\u6307\u6807 */
const MAIN_INDICATORS = [
  { v: "ma", label: "MA" },
  { v: "boll", label: "BOLL" },
  { v: "none", label: "\u65e0" },
];

/* \u526f\u56fe\u6307\u6807 */
const SUB_INDICATORS = [
  { v: "macd", label: "MACD" },
  { v: "kdj", label: "KDJ" },
  { v: "rsi", label: "RSI" },
  { v: "vol", label: "VOL" },
  { v: "wr", label: "WR" },
  { v: "none", label: "\u65e0" },
];

const MA_PERIODS = [5, 10, 20, 60];
const MA_COLORS = ["#ffffff", "#fdbb30", "#c23531", "#91cc75"]; // \u767d/\u9ec4/\u7d2b/\u7eff

// \u6570\u636e\u6765\u6e90\u6807\u8bc6\uff1a\u540e\u7aef source \u5b57\u6bb5 -> \u53cb\u597d\u4e2d\u6587
const SRC_LABEL = { eltdx: "\u901a\u8fbe\u4fe1(TDX)\u884c\u60c5", broker: "\u5238\u5546", cache: "\u672c\u5730\u7f13\u5b58", cache_stale: "\u672c\u5730\u7f13\u5b58(\u8fc7\u671f)" };

/* ======================== \u5de5\u5177\u51fd\u6570 ======================== */
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

function toDate(s) {
  if (!s) return "";
  return String(s).slice(0, 10);
}

function toTimeStr(s) {
  if (!s) return "";
  const str = String(s);
  return str.length >= 16 ? str.slice(11, 16) : str;
}

// \u7528\u5b9e\u65f6 tick \u5c31\u5730\u52337\u65b0\u6700\u540e\u4e00\u6839 K \u7ebf
function applyTick(bars, tick) {
  if (!bars || bars.length === 0 || !tick || tick.last == null) return bars;
  const arr = bars.slice();
  const last = { ...arr[arr.length - 1] };
  const v = Number(tick.last);
  const open = Number(last.open);
  last.close = v;
  last.high = Math.max(Number(last.high) || v, open, v);
  last.low = Math.min(Number(last.low) >= 0 ? Number(last.low) : v, open, v);
  if (tick.volume != null) last.volume = Number(tick.volume);
  arr[arr.length - 1] = last;
  return arr;
}

/* MA \u5747\u7ebf\u8ba1\u7b97 */
function calcMA(data, period) {
  const result = [];
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += data[j];
    result.push(sum / period);
  }
  return result;
}

/* MACD \u8ba1\u7b97 (12,26,9) */
function calcMACD(closePrices) {
  const ema12 = calcEMA(closePrices, 12);
  const ema26 = calcEMA(closePrices, 26);
  const dif = ema12.map((v, i) => (v != null && ema26[i] != null) ? v - ema26[i] : null);
  const dea = calcEMA(dif.filter((v) => v != null), 9);
  // DEA \u957f\u5ea6\u8865\u9f50
  const deaFull = [];
  let deaIdx = 0;
  for (let i = 0; i < dif.length; i++) {
    if (dif[i] == null) { deaFull.push(null); continue; }
    deaFull.push(deaIdx < dea.length ? dea[deaIdx++] : null);
  }
  const macdBar = dif.map((d, i) => {
    if (d == null || deaFull[i] == null) return null;
    return (d - deaFull[i]) * 2; // \u67f1\u72b6\u653e\u5927
  });
  return { dif, dea: deaFull, macdBar };
}

function calcEMA(data, period) {
  const result = [];
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < data.length; i++) {
    if (data[i] == null) { result.push(null); continue; }
    if (prev == null) { prev = data[i]; result.push(data[i]); continue; }
    prev = data[i] * k + prev * (1 - k);
    result.push(prev);
  }
  return result;
}

/* KDJ \u8ba1\u7b97 */
function calcKDJ(highs, lows, closes, n = 9) {
  const kArr = [], dArr = [], jArr = [];
  let k = 50, d = 50, j = 50;
  for (let i = 0; i < closes.length; i++) {
    if (i < n - 1) { kArr.push(null); dArr.push(null); jArr.push(null); continue; }
    let hn = -Infinity, ln = Infinity;
    for (let t = i - n + 1; t <= i; t++) {
      hn = Math.max(hn, highs[t]);
      ln = Math.min(ln, lows[t]);
    }
    const rsv = hn === ln ? 50 : ((closes[i] - ln) / (hn - ln)) * 100;
    k = 2 / 3 * k + 1 / 3 * rsv;
    d = 2 / 3 * d + 1 / 3 * k;
    j = 3 * k - 2 * d;
    kArr.push(k); dArr.push(d); jArr.push(j);
  }
  return { k: kArr, d: dArr, j: jArr };
}

/* RSI \u8ba1\u7b97 */
function calcRSI(closes, period = 14) {
  const result = [];
  let avgGain = 0, avgLoss = 0;
  for (let i = 0; i < closes.length; i++) {
    if (i < period) { result.push(null); continue; }
    if (i === period) {
      let g = 0, l = 0;
      for (let j = 1; j <= period; j++) {
        const delta = closes[j] - closes[j - 1];
        if (delta > 0) g += delta; else l -= delta;
      }
      avgGain = g / period; avgLoss = l / period;
    } else {
      const delta = closes[i] - closes[i - 1];
      avgGain = (avgGain * (period - 1) + (delta > 0 ? delta : 0)) / period;
      avgLoss = (avgLoss * (period - 1) + (delta < 0 ? -delta : 0)) / period;
    }
    result.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));
  }
  return result;
}

/* BOLL \u8ba1\u7b97 */
function calcBOLL(closes, n = 20, m = 2) {
  const mid = calcMA(closes, n);
  const std = [];
  for (let i = 0; i < closes.length; i++) {
    if (mid[i] == null) { std.push(null); continue; }
    let sum = 0, cnt = 0;
    for (let j = i - n + 1; j <= i; j++) {
      if (closes[j] != null) { sum += (closes[j] - mid[i]) ** 2; cnt++; }
    }
    std.push(cnt > 0 ? Math.sqrt(sum / cnt) * m : null);
  }
  const upper = mid.map((v, i) => v != null && std[i] != null ? v + std[i] : null);
  const lower = mid.map((v, i) => v != null && std[i] != null ? v - std[i] : null);
  return { upper, mid, lower };
}

/* WR \u5a01\uu5349\u6307\u6807 */
function calcWR(highs, lows, closes, n = 14) {
  const result = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < n - 1) { result.push(null); continue; }
    let hn = -Infinity, ln = Infinity;
    for (let j = i - n + 1; j <= i; j++) {
      hn = Math.max(hn, highs[j]); ln = Math.min(ln, lows[j]);
    }
    result.push(hn === ln ? 50 : (hn - closes[i]) / (hn - ln) * (-100));
  }
  return result;
}

/* ======================== \u4e3b\u7ec4\u4ef6 ======================== */
export default function MarketData() {
  const { activeId, activeBroker } = useBroker();
  const [code, setCode] = useState("600519.SH");
  const [input, setInput] = useState("600519.SH");
  const [period, setPeriod] = useState("1d");
  const [adj, setAdj] = useState("");
  const [count, setCount] = useState(120);
  const [bars, setBars] = useState([]);
  const [tick, setTick] = useState(null);
  const [stockInfo, setStockInfo] = useState(null);
  const [series, setSeries] = useState([]);
  const [slip, setSlip] = useState(null);
  const [meta, setMeta] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [wsState, setWsState] = useState("connecting");
  const [mainInd, setMainInd] = useState("ma");   // \u4e3b\u56fe\u6307\u6807
  const [subInd, setSubInd] = useState("macd");   // \u526f\u56fe\u6307\u6807

  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const timerRef = useRef(null);
  const wantSymRef = useRef(code);
  const MAX_RETRIES = 10;

  // ---------------- K \u7ebf\u52a0\u8f7d ----------------
  async function loadKline(force) {
    const sym = code || input.trim();
    if (!sym) return;
    setErr(""); setLoading(true);
    try {
      const params = { code: sym, period, count, conn_id: activeId };
      if (force) params.force = true;
      if (adj) params.adj = adj;
      const r = await api.marketKline(params);
      setBars(r.bars || []);
      setMeta({ source: r.source, cached_at: r.cached_at, note: r.note, count: r.count });
    } catch (e) {
      setErr(e.message); setBars([]); setMeta(null);
    } finally { setLoading(false); }
  }

  useEffect(() => {
    if (code) loadKline(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period, count, adj]);

  // ---------------- WebSocket ----------------
  function scheduleReconnect() {
    if (timerRef.current) return;
    if (retryRef.current >= MAX_RETRIES) {
      setWsState("offline");
      setErr(`\u540e\u7aef\u8fde\u63a5\u5931\u8d25\uff0c\u5df2\u505c\u6b62\u81ea\u52a8\u91cd\u8fde\uff08\u5df2\u8fbe ${MAX_RETRIES} \u6b21\u4e0a\u9650\uff09\u3002`);
      return;
    }
    const delay = Math.min(0.5 * Math.pow(2, retryRef.current), 15);
    retryRef.current += 1;
    setWsState("reconnecting");
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      connect(wantSymRef.current);
    }, delay * 1000);
  }

  function applyQuote(q) {
    if (!q) return;
    setTick(q);
    const last = q.last;
    if (last != null) {
      setSeries((prev) => [...prev.slice(-59), { ts: q.ts || new Date().toLocaleTimeString(), last }]);
    }
    setBars((prev) => applyTick(prev, q));
  }

  function connect(sym) {
    wantSymRef.current = sym;
    if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } wsRef.current = null; }
    retryRef.current = 0;
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => {
      retryRef.current = 0;
      setWsState("connected"); setErr("");
      ws.send(JSON.stringify({ action: "subscribe", codes: [sym] }));
    };
    ws.onmessage = (e) => {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      const symc = wantSymRef.current;
      const takeItem = (it) => { if (it && it.code === symc) applyQuote(it); };
      if (msg.type === "quotes" && Array.isArray(msg.data?.items)) {
        msg.data.items.forEach(takeItem);
      } else if (msg.type === "quotes_replay" && Array.isArray(msg.data?.items)) {
        msg.data.items.forEach(takeItem);
      } else if (msg.type === "quote") {
        takeItem(msg.data);
      } else if (msg.type === "snapshot" && msg.data?.quotes) {
        const snap = msg.data.quotes[symc];
        if (snap) applyQuote(snap);
      }
    };
    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null;
      setWsState("offline");
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }

  function fetchStockInfo(sym) {
    api.marketStockInfo({ code: sym, conn_id: activeId })
      .then((r) => setStockInfo(r.data || r))
      .catch(() => {});
  }

  function subscribe() {
    const sym = input.trim();
    if (!sym) return;
    setCode(sym); setSeries([]); setTick(null); setStockInfo(null); setErr("");
    connect(sym);
    loadKline(true);
    fetchStockInfo(sym);
    api.get("/account/slippage", { code: sym, conn_id: activeId })
      .then(setSlip).catch((e) => setErr(e.message));
  }

  useEffect(() => {
    connect(code);
    fetchStockInfo(code);
    api.get("/account/slippage", { code, conn_id: activeId }).then(setSlip).catch(() => {});
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (wsRef.current) wsRef.current.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // ---------------- ECharts \u914d\u7f6e\u6784\u5efa ----------------
  const chartOption = useMemo(() => {
    const times = bars.map((b) => toDate(b.time));
    const candle = bars.map((b) => [Number(b.open), Number(b.close), Number(b.low), Number(b.high)]);
    const vol = bars.map((b) => Number(b.volume) || 0);
    const closes = bars.map((b) => Number(b.close));
    const highs = bars.map((b) => Number(b.high));
    const lows = bars.map((b) => Number(b.low));

    // VOL \u67f1\u989c\u8272\uff08\u6da8\u7ea2\u8dcc\u7eff\uff09
    const volColors = bars.map((b) =>
      Number(b.close) >= Number(b.open) ? "#ef4d56" : "#29c08a"
    );

    /** @type {import("echarts").EChartsOption} */
    const opt = {
      backgroundColor: "transparent",
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: "rgba(18,22,32,.92)",
        borderColor: "#2c3850",
        textStyle: { color: "#e6ecf5", fontSize: 12 },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [],
      xAxis: [],
      yAxis: [],
      series: [],
      legend: {
        data: [],
        top: 2,
        textStyle: { color: "#8a97ad", fontSize: 11 },
        itemWidth: 16, itemHeight: 8,
      },
    };

    // ---- Grid 0: K\u7ebf\u4e3b\u56fe ----
    opt.grid[0] = { left: 55, right: 8, top: 24, height: "54%" };
    opt.xAxis[0] = {
      type: "category", data: times, boundaryGap: true,
      axisLine: { lineStyle: { color: "#2c3850" } },
      axisLabel: { show: false, color: "#5a6a82", fontSize: 10 },
      splitLine: { show: false },
      axisTick: { show: false },
    };
    opt.yAxis[0] = {
      scale: true, position: "right",
      axisLine: { lineStyle: { color: "#2c3850" } },
      axisLabel: { color: "#5a6a82", fontSize: 10, formatter: "{value}" },
      splitLine: { lineStyle: { color: "#1a2233", type: "dashed" } },
    };

    // K\u7ebf\u8721\u70db
    opt.series.push({
      name: "K\u7ebf",
      type: "candlestick",
      data: candle,
      itemStyle: { color: "#ef4d56", color0: "#29c08a", borderColor: "#ef4d56", borderColor0: "#29c08a" },
    });

    // \u4e3b\u56fe\u6307\u6807\u53e0\u52a0
    if (mainInd === "ma" && bars.length > 0) {
      MA_PERIODS.forEach((p, idx) => {
        const maData = calcMA(closes, p);
        opt.legend.data.push(`MA${p}`);
        opt.series.push({
          name: `MA${p}`,
          type: "line",
          data: maData,
          smooth: true,
          symbol: "none",
          lineStyle: { width: 1, color: MA_COLORS[idx] },
        });
      });
    } else if (mainInd === "boll" && bars.length > 0) {
      const boll = calcBOLL(closes);
      opt.legend.data.push("BOLL-UP", "BOLL-MID", "BOLL-LOW");
      opt.series.push(
        { name: "BOLL-UP", type: "line", data: boll.upper, symbol: "none", lineStyle: { width: 1, color: "#c23531" } },
        { name: "BOLL-MID", type: "line", data: boll.mid, symbol: "none", lineStyle: { width: 1, color: "#91cc75" } },
        { name: "BOLL-LOW", type: "line", data: boll.lower, symbol: "none", lineStyle: { width: 1, color: "#c23531" } },
      );
    }

    // ---- Grid 1: VOL \u6210\u4ea4\u91cf ----
    const hasVolSub = subInd === "vol" || subInd === "macd" || subInd === "kdj" || subInd === "rsi" || subInd === "wr";
    if (hasVolSub) {
      opt.grid[1] = { left: 55, right: 8, top: "74%", height: "13%" };
      opt.xAxis[1] = {
        type: "category", gridIndex: 1, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 },
        axisTick: { show: false }, splitLine: { show: false },
      };
      opt.yAxis[1] = {
        gridIndex: 1, scale: true, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false },
        splitLine: { show: false },
      };

      if (subInd !== "none") {
        opt.legend.data.push("\u6210\u4ea4\u91cf");
        opt.series.push({
          name: "\u6210\u4ea4\u91cf",
          type: "bar",
          xAxisIndex: 1, yAxisIndex: 1, data: vol,
          itemStyle: (params) => ({
            color: volColors[params.dataIndex],
          }),
        });
      }
    }

    // ---- Grid 2: \u526f\u56fe\u6307\u6807 (MACD/KDJ/RSI/WR) ----
    if (subInd === "macd" && bars.length > 0) {
      const macd = calcMACD(closes);
      opt.grid[2] = { left: 55, right: 8, top: "89%", height: "11%" };
      opt.xAxis[2] = {
        type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 },
        axisTick: { show: false }, splitLine: { show: false },
      };
      opt.yAxis[2] = {
        gridIndex: 2, scale: true, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false },
        splitLine: { show: false },
      };
      opt.legend.data.push("DIF", "DEA", "MACD");
      opt.series.push(
        { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: macd.dif, symbol: "none", lineStyle: { width: 1, color: "#ffffff" } },
        { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: macd.dea, symbol: "none", lineStyle: { width: 1, color: "#fdbb30" } },
        { name: "MACD", type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: macd.macdBar,
          itemStyle: (params) => ({ color: (macd.macdBar[params.dataIndex] || 0) >= 0 ? "#ef4d56" : "#29c08a" }) },
      );
    } else if (subInd === "kdj" && bars.length > 0) {
      const kdj = calcKDJ(highs, lows, closes);
      opt.grid[2] = { left: 55, right: 8, top: "89%", height: "11%" };
      opt.xAxis[2] = {
        type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 },
        axisTick: { show: false }, splitLine: { show: false },
      };
      opt.yAxis[2] = { gridIndex: 2, scale: true, min: 0, max: 100, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("K", "D", "J");
      opt.series.push(
        { name: "K", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: kdj.k, symbol: "none", lineStyle: { width: 1, color: "#ef4d56" } },
        { name: "D", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: kdj.d, symbol: "none", lineStyle: { width: 1, color: "#fdbb30" } },
        { name: "J", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: kdj.j, symbol: "none", lineStyle: { width: 1, color: "#4f8cff" } },
      );
    } else if (subInd === "rsi" && bars.length > 0) {
      const rsi6 = calcRSI(closes, 6);
      const rsi12 = calcRSI(closes, 12);
      opt.grid[2] = { left: 55, right: 8, top: "89%", height: "11%" };
      opt.xAxis[2] = {
        type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 },
        axisTick: { show: false }, splitLine: { show: false },
      };
      opt.yAxis[2] = { gridIndex: 2, scale: true, min: 0, max: 100, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("RSI6", "RSI12");
      opt.series.push(
        { name: "RSI6", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: rsi6, symbol: "none", lineStyle: { width: 1, color: "#ef4d56" } },
        { name: "RSI12", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: rsi12, symbol: "none", lineStyle: { width: 1, color: "#4f8cff" } },
      );
    } else if (subInd === "wr" && bars.length > 0) {
      const wr = calcWR(highs, lows, closes);
      opt.grid[2] = { left: 55, right: 8, top: "89%", height: "11%" };
      opt.xAxis[2] = {
        type: "category", gridIndex: 2, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#5a6a82", fontSize: 10 },
        axisTick: { show: false }, splitLine: { show: false },
      };
      opt.yAxis[2] = { gridIndex: 2, scale: true, splitNumber: 2,
        axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } };
      opt.legend.data.push("WR");
      opt.series.push(
        { name: "WR", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: wr, symbol: "none", lineStyle: { width: 1, color: "#91cc75" } },
      );
    }

    return opt;
  }, [bars, mainInd, subInd]);

  // \u5206\u65f6\u56fe option
  const priceOption = useMemo(() => ({
    backgroundColor: "transparent",
    grid: { left: 50, right: 8, top: 12, bottom: 24 },
    xAxis: { type: "category", data: series.map((d) => d.ts), axisLabel: { show: false, fontSize: 10 } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#1a2233", type: "dashed" } },
      axisLabel: { color: "#5a6a82", fontSize: 10 } },
    tooltip: { trigger: "axis", backgroundColor: "rgba(18,22,32,.92)", borderColor: "#2c3850", textStyle: { color: "#e6ecf5", fontSize: 12 } },
    series: [{
      type: "line", smooth: true, showSymbol: false, data: series.map((d) => d.last),
      lineStyle: { color: "#4f8cff", width: 1 },
      areaStyle: { color: "rgba(79,140,255,.06)" },
    }],
  }), [series]);

  const cls = tick ? (tick.last >= (tick.pre_close || tick.bid || 0) ? "up" : "down") : "";
  const lastBar = bars[bars.length - 1];

  return (
    <div className="market-page">
      {/* \u9876\u90e8\u6807\u9898\u680f */}
      <div className="mp-header">
        <div className="mp-title-row">
          <h2 className="page-title">\u884c\u60c5\u56fe</h2>
          <span className={`ws-badge ws-${wsState}`}>
            {wsState === "connected" ? "\u25cf \u5df2\u8fde\u63a5"
              : wsState === "reconnecting" ? "\u27f3 \u91cd\u8fde\u2026"
              : wsState === "connecting" ? "\u25cb \u8fde\u63a5\u2026" : "\u25cb \u79bb\u7ebf"}
          </span>
        </div>

        {/* \u5de5\u5177\u680f */}
        <div className="mp-toolbar">
          <input className="mp-code-input" value={input} placeholder="\u4ee3\u7801 \u5982 600519.SH"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && subscribe()} />
          <button onClick={subscribe}>\u8ba2\u9605</button>

          <select value={period} onChange={(e) => setPeriod(e.target.value)}>
            {PERIODS.map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
          </select>

          <select value={adj} onChange={(e) => setAdj(e.target.value)}>
            {ADJ_TYPES.map((a) => <option key={a.v} value={a.v}>{a.label}</option>)}
          </select>

          <label className="muted">\u6839\u6570</label>
          <input type="number" className="mp-count-input" min={10} max={1000} value={count}
            onChange={(e) => setCount(Math.max(10, Math.min(1000, +e.target.value || 120)))} />

          <select value={mainInd} onChange={(e) => setMainInd(e.target.value)} title="\u4e3b\u56fe\u6307\u6807">
            {MAIN_INDICATORS.map((ind) => <option key={ind.v} value={ind.v}>{ind.label}</option>)}
          </select>

          <select value={subInd} onChange={(e) => setSubInd(e.target.value)} title="\u526f\u56fe\u6307\u6807">
            {SUB_INDICATORS.map((ind) => <option key={ind.v} value={ind.v}>{ind.label}</option>)}
          </select>

          <button className="ghost btn-sm" onClick={() => loadKline(true)} disabled={loading}>
            {loading ? "\u52a0\u8f7d\u2026" : "\u52337\u65b0"}
          </button>
          <button className="ghost btn-sm" onClick={() => quickTradeNavigate(code, lastBar ? Number(lastBar.close) : (tick ? Number(tick.last) : null))}
                  disabled={!lastBar && !tick}>\u4ea4\u6613</button>
        </div>

        <div className="mp-status">
          <span>{PERIODS.find((p) => p.v === period)?.label}</span>
          <span> \u00b7 \u5171 {bars.length} \u6839</span>
          {meta && (<> \u00b7 \u6570\u636e\u6765\u6e90 <span className={`tag ok src-${meta.source}`}>{SRC_LABEL[meta.source] || meta.source}</span></>)}
          {activeBroker && <span> \u00b7 {activeBroker.broker_name}</span>}
        </div>
      </div>

      {err && <div className="toast err">{err}</div>}

      {/* \u5de6\u53f3\u5206\u680f\u5e03\u5c40 */}
      <div className="mp-body">
        {/* \u5de6\u4fa7\uff1a\u56fe\u8868\u533a */}
        <div className="mp-chart-area">
          {/* K\u7ebf+\u6210\u4ea4\u91cf+\u6307\u6807 */}
          <div className="card mp-chart-card">
            {bars.length > 0
              ? <Chart option={chartOption} height={520} />
              : <div className="chart-empty">
                  {err ? "\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u4ee3\u7801\u4e0e\u52378\u5546\u8fde\u63a5" : "\u6682\u65e0\u6570\u636e\uff0c\u8f93\u5165\u4ee3\u7801\u540e\u70b9\u51fb\u8ba2\u9605"}
                </div>}
          </div>

          {/* \u5e95\u90e8\u6307\u6807\u5feb\u6377\u680f */}
          <div className="indicator-bar">
            {SUB_INDICATORS.map((ind) => (
              <button key={ind.v} className={`ib-btn ${subInd === ind.v ? "active" : ""}`}
                onClick={() => setSubInd(ind.v)}>{ind.label}</button>
            ))}
          </div>

          {/* \u5206\u65f6\u56fe */}
          <div className="card" style={{ marginTop: 12 }}>
            <h3 style={{ margin: "0 0 8px", fontSize: 12, color: "var(--text-dim)" }}>\u5b9e\u65f6\u4ef7\u683c\u8d70\u52bf</h3>
            <Chart option={priceOption} height={160} />
          </div>
        </div>

        {/* \u53f3\u4fa7\uff1a\u62a5\u4ef7\u9762\u677f */}
        <div className="mp-quote-area">
          <QuotePanel tick={tick} stockInfo={stockInfo} code={code} bars={bars} />
        </div>
      </div>

      {/* \u6ed1\u70b9\u5206\u6790\uff08\u653e\u5e95\u90e8\uff09 */}
      <div className="card" style={{ marginTop: 12 }}>
        <h3 style={{ margin: "0 0 8px", fontSize: 12, color: "var(--text-dim)" }}>\u6ed1\u70b9\u5206\u6790</h3>
        {slip ? (
          slip.samples.length === 0
            ? <p className="muted" style={{ fontSize: 12 }}>\u8be5\u6807\u7684\u6682\u65e0\u6210\u4ea4\u8bb0\u5f55</p>
            : (
              <table style={{ fontSize: 12 }}>
                <thead><tr><th>\u65f6\u95f4</th><th>\u65b9\u5411</th><th>\u6210\u4ea4\u4ef7</th><th>\u5bf9\u5f00\u76d8</th><th>\u5bf9\u6536\u76d8</th><th>\u5bf9\u5747\u4ef7</th></tr></thead>
                <tbody>
                  {slip.samples.slice(-10).map((s, i) => (
                    <tr key={i}>
                      <td>{s.time}</td>
                      <td className={s.side === "buy" ? "up" : "down"}>{s.side === "buy" ? "\u4e70" : "\u5356"}</td>
                      <td>{s.price}</td>
                      <td className={(s.slippage_open_bps || 0) >= 0 ? "up" : "down"}>{s.slippage_open_bps}</td>
                      <td className={(s.slippage_close_bps || 0) >= 0 ? "up" : "down"}>{s.slippage_close_bps}</td>
                      <td className={(s.slippage_avg_bps || 0) >= 0 ? "up" : "down"}>{s.slippage_avg_bps}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
        ) : <p className="muted" style={{ fontSize: 12 }}>\u52a0\u8f7d\u4e2d\u2026</p>}
        {slip && slip.samples.length > 0 && (
          <p className="muted" style={{ marginTop: 8, fontSize: 11 }}>
            \u5e73\u5747\u7edd\u5bf9\u6ed1\u70b9\uff08\u5bf9\u5747\u4ef7\uff09\uff1a{slip.avg_abs_slippage_avg_bps} bps
          </p>
        )}
      </div>
    </div>
  );
}
