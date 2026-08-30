import { useMemo } from "react";
// 金额格式化统一走 lib/format.js（原本地副本万档 toFixed(0)、<1万档 toFixed(2)，
// 与其余三处口径全不一致，是同一金额跨页显示不同的直接来源）
import { fmtAmount } from "../lib/format.js";

/* 通达信风格右侧报价面板 */
export default function QuotePanel({ tick, stockInfo, code, bars }) {
  const lastBar = bars?.[bars.length - 1];
  const preClose = lastBar ? Number(lastBar.close)
    : (tick?.preClose != null ? Number(tick.preClose)
      : (tick?.lastClose != null ? Number(tick.lastClose) : null));
  const last = tick?.last != null ? Number(tick.last)
    : (lastBar ? Number(lastBar.close) : null);
  const open = tick?.open != null ? Number(tick.open) : null;
  const high = tick?.high != null ? Number(tick.high) : null;
  const low = tick?.low != null ? Number(tick.low) : null;

  const changeAmt = last != null && preClose != null ? last - preClose : null;
  const changePct = changeAmt != null && preClose != null && preClose !== 0
    ? (changeAmt / preClose) * 100 : null;

  const cls = changePct != null ? (changePct >= 0 ? "up" : "down") : "";

  // 五档买卖盘（真实数组：买一~买五 / 卖一~卖五）
  const bids = tick?.bids || [];
  const asks = tick?.asks || [];
  const hasBook = bids.length > 0 || asks.length > 0;

  // 基本信息
  const name = stockInfo?.name || tick?.name || code;
  const board = stockInfo?.board || tick?.board || "—";
  const exchange = stockInfo?.exchange || tick?.exchange || "—";
  const industry = stockInfo?.industry || tick?.industry || "";
  const concepts = stockInfo?.concepts || tick?.concepts || [];
  // 数据来源标识：stock-info 优先（名称/行业/涨跌停来源），回退实时 tick
  const srcRaw = stockInfo?.source || tick?.source || "";
  const SRC_LABEL = { eltdx: "TDX行情", broker: "券商", cache: "本地缓存" };
  const srcLabel = SRC_LABEL[srcRaw] || "—";
  const srcKey = srcRaw || "unknown";
  const highLimit = stockInfo?.high_limit != null ? Number(stockInfo.high_limit)
    : (tick?.high_limit != null ? Number(tick.high_limit) : null);
  const lowLimit = stockInfo?.low_limit != null ? Number(stockInfo.low_limit)
    : (tick?.low_limit != null ? Number(tick.low_limit) : null);

  // 逐笔成交
  const trades = tick?.trades || [];

  // 五档行（卖五→卖一，买一→买五），附量能配比（比例色块用）
  const bookRows = useMemo(() => {
    const asksArr = asks.map((a, i) => ({ side: "ask", level: i + 1, price: a.price, volume: a.volume }));
    const bidsArr = bids.map((b, i) => ({ side: "bid", level: i + 1, price: b.price, volume: b.volume }));
    return {
      asks: asksArr,
      bids: bidsArr,
      rows: [...[...asksArr].reverse() /* 卖五→卖一 */, ...bidsArr],
      maxAskVol: Math.max(1, ...asksArr.map((a) => Number(a.volume) || 0)),
      maxBidVol: Math.max(1, ...bidsArr.map((b) => Number(b.volume) || 0)),
    };
  }, [asks, bids]);

  // 委比/委差（通达信口径，由五档派生）：委差=委买总量-委卖总量；委比=差/和×100%
  const sumBuyVol = useMemo(() => bids.reduce((s, b) => s + (Number(b.volume) || 0), 0), [bids]);
  const sumSellVol = useMemo(() => asks.reduce((s, a) => s + (Number(a.volume) || 0), 0), [asks]);
  const weichah = sumBuyVol - sumSellVol;                                   // 委差（手）
  const weibi = (sumBuyVol + sumSellVol) > 0
    ? (weichah / (sumBuyVol + sumSellVol)) * 100 : null;                    // 委比（%）
  const inside = tick?.inside != null ? Number(tick.inside) : null;         // 内盘（手）
  const outside = tick?.outside != null ? Number(tick.outside) : null;     // 外盘（手）
  const currentHand = tick?.current_hand != null ? Number(tick.current_hand) : null; // 现量
  const fmtHands = (v) => v == null ? "—" : (v >= 1e4 ? (v / 1e4).toFixed(1) + "万" : v.toLocaleString());

  const { rows: bookRowsList, maxAskVol, maxBidVol } = bookRows;

  return (
    <div className="quote-panel">
      {/* 头部：名称 + 代码 + 板块 + 数据来源 */}
      <div className="qp-header">
        <div className="qp-name-block">
          <span className="qp-name">{name || "—"}</span>
          <span className="qp-code">{code || "—"}</span>
        </div>
        <div className="qp-header-tags">
          <span className="qp-board" title="所属板块">{board}</span>
          <span className={`qp-source source-${srcKey}`} title="数据来源（TDX行情 / 券商 / 本地缓存）">
            {srcLabel}
          </span>
        </div>
      </div>

      {/* 最新价 + 涨跌幅 */}
      <div className="qp-price-row">
        <span className={`qp-price ${cls}`}>{last != null ? last.toFixed(2) : "—"}</span>
        <div className="qp-change">
          <span className={cls}>{changeAmt != null ? (changeAmt >= 0 ? "+" : "") + changeAmt.toFixed(2) : "—"}</span>
          <span className={cls}>{changePct != null ? (changePct >= 0 ? "+" : "") + changePct.toFixed(2) + "%" : "—"}</span>
        </div>
      </div>

      {/* 基本信息 */}
      <div className="qp-section-title">基本信息</div>
      <div className="qp-stats">
        <div className="qp-stat-row"><span className="qp-label">交易所</span><span>{exchange}</span></div>
        <div className="qp-stat-row"><span className="qp-label">板块</span><span>{board}</span></div>
        <div className="qp-stat-row"><span className="qp-label">行业</span><span className="up-accent">{industry || "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">今开</span><span>{open != null ? open.toFixed(2) : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">最高</span><span className="up">{high != null ? high.toFixed(2) : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">最低</span><span className="down">{low != null ? low.toFixed(2) : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">昨收</span><span>{preClose != null ? preClose.toFixed(2) : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">涨停</span><span className="up">{highLimit != null ? highLimit.toFixed(2) : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">跌停</span><span className="down">{lowLimit != null ? lowLimit.toFixed(2) : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">成交量</span><span>{tick?.volume != null ? Number(tick.volume).toLocaleString() : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">成交额</span><span>{fmtAmount(tick?.amount)}</span></div>
        <div className="qp-stat-row"><span className="qp-label">现量</span><span>{currentHand != null ? currentHand.toLocaleString() : "—"}</span></div>
        <div className="qp-stat-row"><span className="qp-label">内盘</span><span className="down">{fmtHands(inside)}</span></div>
        <div className="qp-stat-row"><span className="qp-label">外盘</span><span className="up">{fmtHands(outside)}</span></div>
        <div className="qp-stat-row"><span className="qp-label">委比</span>
          <span className={weibi == null ? "" : weibi >= 0 ? "up" : "down"}>
            {weibi == null ? "—" : (weibi >= 0 ? "+" : "") + weibi.toFixed(2) + "%"}
          </span></div>
        <div className="qp-stat-row"><span className="qp-label">委差</span>
          <span className={weichah == null ? "" : weichah >= 0 ? "up" : "down"}>
            {weichah == null ? "—" : fmtHands(weichah)}
          </span></div>
      </div>

      {/* 概念题材（通达信题材，来自 eltdx F10） */}
      {concepts.length > 0 && (
        <>
          <div className="qp-section-title">概念题材</div>
          <div className="qp-concepts">
            {concepts.map((c, i) => (
              <span key={i} className="qp-concept-tag">{c}</span>
            ))}
          </div>
        </>
      )}

      {/* 五档买卖盘 */}
      <div className="qp-section-title">五档买卖盘</div>
      {hasBook ? (
        <table className="qp-book-table">
          <tbody>
            {bookRowsList.map((r, idx) => {
              // 量能配比：该档量占本侧五档最大量的百分比
              const maxVol = r.side === "ask" ? maxAskVol : maxBidVol;
              const pct = r.volume != null ? Math.min(100, (Number(r.volume) / maxVol) * 100) : 0;
              return (
                <tr key={idx} className={`${r.side === "ask" ? "qa-ask-row" : "qa-bid-row"}`}>
                  <td className={`qa-side ${r.side === "ask" ? "down" : "up"}`} aria-label={r.side === "ask" ? "卖 SELL" : "买 BUY"}>
                    {r.side === "ask" ? "卖" : "买"}{r.level}
                    <span className="qa-lang-hint">{r.side === "ask" ? "SELL" : "BUY"}</span>
                  </td>
                  <td className={`qa-price ${r.side === "ask" ? "down" : "up"}`}>
                    {r.price != null ? Number(r.price).toFixed(2) : "—"}
                  </td>
                  <td className="qa-volwrap">
                    <span className="qa-volbar" style={{ width: `${pct}%`, background: r.side === "ask" ? "var(--down-bar)" : "var(--up-bar)" }} aria-hidden="true" />
                    <span className="qa-vol">{r.volume != null ? Number(r.volume).toLocaleString() : "—"}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className="qp-book-empty">暂无五档数据（未连接券商或无实时盘口）</div>
      )}

      {/* 逐笔成交 */}
      <div className="qp-section-title">逐笔成交</div>
      <div className="qp-trades">
        {trades.length > 0 ? (
          trades.slice(-12).map((t, i) => (
            <div key={i} className={`qp-trade-row ${t.side === "buy" ? "up" : t.side === "sell" ? "down" : ""}`}>
              <span className="qt-time">{t.time || t.ts || ""}</span>
              <span className="qt-price">{t.price != null ? Number(t.price).toFixed(2) : "—"}</span>
              <span className="qt-vol">{t.volume != null ? t.volume : "—"}</span>
              <span className="qt-side">{t.side === "buy" ? "买" : t.side === "sell" ? "卖" : "—"}</span>
            </div>
          ))
        ) : (
          <p className="muted" style={{ fontSize: 11, padding: "6px 0" }}>暂无逐笔数据</p>
        )}
      </div>
    </div>
  );
}
