// 多维摘要抽屉面板（自 MarketData.jsx 原样拆出，行为零变更）：
// 资金/股本/涨跌停 + 买卖力道 + 资金流日内回放（G3 落库观测的快照序列）。
// 打开抽屉时拉一次回放（不轮询）；无快照显「暂无快照」引导，绝不估算填充。
import { useEffect, useMemo, useState } from "react";
import { api } from "../../api.js";
import Chart from "../Chart.jsx";
import { PALETTE } from "../../lib/chartPalette.js";
import { toBoards } from "./boards.js";

export default function MfPanel({ code, mf, mfErr, cap, turnover, industryName,
  conceptList, mfNet, mfInside, mfOutside, refreshToken, refreshMf }) {
  /* ---------- C2：资金流日内回放（G3 落库观测的快照序列） ---------- */
  const [replay, setReplay] = useState(null);
  const [replayErr, setReplayErr] = useState("");
  useEffect(() => {
    if (!code) return;
    let alive = true;
    setReplay(null); setReplayErr("");
    api.moneyflowReplay({ code, limit: 240 })
      .then((r) => { if (alive) setReplay(r); })
      .catch((e) => { if (alive) setReplayErr(e.message || "回放数据暂不可用"); });
    return () => { alive = false; };
  }, [code, refreshToken]);
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
        lineStyle: { width: 1.5, color: rows[rows.length - 1].net >= 0 ? PALETTE.up : PALETTE.down },
        areaStyle: { color: "rgba(79,140,255,.10)" },
      }],
    };
  }, [replay]);

  const limitInfo = cap?.limits?.[code] || null;
  const circShares = cap?.shares?.[code]?.circulating_shares != null
    ? Number(cap.shares[code].circulating_shares) : null;

  return (
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
  );
}
