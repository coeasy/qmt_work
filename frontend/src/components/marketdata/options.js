// 图表 option 构建器（自 MarketData.jsx 原样拆出的纯函数，行为零变更）：
// - buildTickOption：真分时（TDX 同款）价格/均价/分钟量三栏
// - buildKlineOption：K 线 + 主图指标(MA/BOLL) + 成交量 + 副图指标(MACD/KDJ/RSI/WR)
import { PALETTE } from "../../lib/chartPalette.js";
import { indicatorKey } from "../../lib/indicators.js";
import { MA_PERIODS, MA_COLORS, toDate } from "./constants.js";

/* ======================== 图：真分时（通达信同款） ======================== */
// 数据：/market/minutes 分钟曲线（eltdx TDX 分时，含均价线）；
// 实时：QuoteHub tick 就地改写最后一个点，60s 轮询整体刷新。
// 无分时数据时回退为快照基准的占位提示（绝不伪造曲线）。
// preCloseOf 由调用方注入（hooks/useMarket.js 导出）。
export function buildTickOption({ minutesData, minutesErr, tick, stockInfo, preCloseOf }) {
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
  const priceData = pts.map((p) => {
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
    return { value: Number(p.volume) || 0, itemStyle: { color: priceArr[i] >= prev ? PALETTE.up : PALETTE.down } };
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
  const lineColor = upNow ? PALETTE.up : PALETTE.down;
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
        lineStyle: { width: 1, color: PALETTE.warn } },
      // B2：源层个股/板块 K 线 volume 均为 volume_lots（手），图例带单位防误读
      { name: "分钟量(手)", type: "bar", xAxisIndex: 1, yAxisIndex: 2, data: volData },
    ],
  };
}

/* ======================== 图：K 线 + 主/副指标 ======================== */
export function buildKlineOption({ bars, mainInd, subInd, err, indData, drawings }) {
  if (!bars.length) {
    return { title: { text: err ? "加载失败，请检查代码或券商连接" : "暂无数据，输入代码后点击订阅", left: "center", top: "center", textStyle: { color: "#8aa0c0" } } };
  }
  const times = bars.map((b) => toDate(b.time || b.date));
  const candle = bars.map((b) => [Number(b.open), Number(b.close), Number(b.low), Number(b.high)]);
  const vols = bars.map((b) => Number(b.volume) || 0);
  const volColors = bars.map((b) => Number(b.close) >= Number(b.open) ? PALETTE.up : PALETTE.down);

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
    itemStyle: { color: PALETTE.up, color0: PALETTE.down, borderColor: PALETTE.up, borderColor0: PALETTE.down },
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
      { name: "BOLL-UP", type: "line", data: b.upper || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.danger } },
      { name: "BOLL-MID", type: "line", data: b.mid || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.accent2 } },
      { name: "BOLL-LOW", type: "line", data: b.lower || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.danger } },
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

  const SUB_GRID = (opt2) => {
    // bottom 用固定像素（而非百分比）：无论图表容器多高，都稳定留出滑块空间
    opt2.grid[2] = { left: 55, right: 8, top: "82%", bottom: 26 };
    opt2.xAxis[2] = { type: "category", gridIndex: 2, data: times, boundaryGap: true,
      axisLine: { lineStyle: { color: "#2c3850" } },
      axisLabel: { color: "#5a6a82", fontSize: 10 }, axisTick: { show: false }, splitLine: { show: false } };
  };
  const SUB_YAXIS = (opt2, extra = {}) => ({
    gridIndex: 2, scale: true, splitNumber: 2, axisLabel: { show: false },
    axisTick: { show: false }, splitLine: { show: false }, ...extra,
  });

  if (subInd === "macd") {
    const m = indData[indicatorKey("macd", {})] || {};
    SUB_GRID(opt);
    opt.yAxis[2] = SUB_YAXIS(opt);
    opt.legend.data.push("DIF", "DEA", "MACD");
    const mBar = m.bar || [];
    opt.series.push(
      { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: m.dif || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.text } },
      { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: m.dea || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.warn } },
      { name: "MACD", type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: mBar,
        itemStyle: (p) => ({ color: (mBar[p.dataIndex] || 0) >= 0 ? PALETTE.up : PALETTE.down }) },
    );
  } else if (subInd === "kdj") {
    const k = indData[indicatorKey("kdj", {})] || {};
    SUB_GRID(opt);
    opt.yAxis[2] = SUB_YAXIS(opt, { min: 0, max: 100 });
    opt.legend.data.push("K", "D", "J");
    opt.series.push(
      { name: "K", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: k.k || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.up } },
      { name: "D", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: k.d || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.warn } },
      { name: "J", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: k.j || [], symbol: "none", lineStyle: { width: 1, color: PALETTE.accent } },
    );
  } else if (subInd === "rsi") {
    const r6 = (indData[indicatorKey("rsi", { win: 6 })] || {}).rsi || [];
    const r12 = (indData[indicatorKey("rsi", { win: 12 })] || {}).rsi || [];
    SUB_GRID(opt);
    opt.yAxis[2] = SUB_YAXIS(opt, { min: 0, max: 100 });
    opt.legend.data.push("RSI6", "RSI12");
    opt.series.push(
      { name: "RSI6", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: r6, symbol: "none", lineStyle: { width: 1, color: PALETTE.up } },
      { name: "RSI12", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: r12, symbol: "none", lineStyle: { width: 1, color: PALETTE.accent } },
    );
  } else if (subInd === "wr") {
    const w = (indData[indicatorKey("wr", {})] || {}).wr || [];
    SUB_GRID(opt);
    opt.yAxis[2] = SUB_YAXIS(opt);
    opt.legend.data.push("WR");
    opt.series.push({ name: "WR", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: w,
      symbol: "none", lineStyle: { width: 1, color: PALETTE.accent2 } });
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
      handleStyle: { color: PALETTE.accent, borderColor: PALETTE.accent },
      moveHandleStyle: { color: "#3a4a66" },
      dataBackground: { lineStyle: { color: "#3a4a66" }, areaStyle: { color: "rgba(58,74,102,.35)" } },
      selectedDataBackground: { lineStyle: { color: PALETTE.accent }, areaStyle: { color: "rgba(79,140,255,.25)" } },
      textStyle: { color: "#5a6a82", fontSize: 9 } },
  ];
  // G9-2 画线：水平线 graphic 元素（y 为点击时 convertToPixel 像素；缩放后位置近似）
  if (drawings.length) {
    opt.graphic = drawings.map((d) => ({
      type: "line",
      shape: { x1: 0, y1: d.y, x2: 10000, y2: d.y },
      style: { stroke: PALETTE.warn, lineWidth: 1, lineDash: [4, 4], opacity: 0.8 },
    }));
  }
  return opt;
}
