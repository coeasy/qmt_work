// 成交明细：实时逐笔成交 + 大单标志 + 买卖方向。
// v2 新增独立页面（原 BottomDock 成交明细 Tab 提升）。
import { useEffect, useState, useRef } from "react";
import { useSystemStatus, useServerEvents } from "../../hooks/useSystemWS.js";
import { fmtAmount } from "../../lib/format.js";

const BIG_DEAL_THRESHOLD = 5000000; // 500万

export default function DealFeed() {
  const [code, setCode] = useState("");
  const [subscribedCode, setSubscribedCode] = useState("");
  const [deals, setDeals] = useState([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const listRef = useRef(null);
  const { status } = useSystemStatus();

  // 后端成交事件经系统 WS 到达；订阅全部 deal/fill 事件后按代码过滤。
  // 空代码表示展示全部账户成交流。
  useServerEvents(["deal", "fill", "trade"], (msg) => {
    const d = msg?.data || {};
    if (subscribedCode && d.code !== subscribedCode) return;
    setDeals((prev) => [{ ...d, _t: Date.now() }, ...prev].slice(0, 500));
  });

  // 自动滚动：新成交到达时置顶；autoScroll=false 时保留用户当前位置。
  useEffect(() => {
    if (autoScroll && listRef.current) listRef.current.scrollTop = 0;
  }, [deals, autoScroll]);

  const handleSubscribe = () => {
    const c = code.trim().replace(/\D/g, "");
    if (c.length === 6 || /^\d{6}\.[A-Z]{2}$/.test(code.trim().toUpperCase())) {
      setSubscribedCode(c || code.trim().toUpperCase());
      setDeals([]);
    }
  };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "8px 12px", display: "flex", gap: 8, alignItems: "center", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
        <input
          type="text"
          placeholder="6位或600519.SH"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSubscribe()}
          style={{ width: 150, padding: "4px 8px", background: "var(--bg3)", border: "1px solid var(--border)", borderRadius: 4, color: "var(--text)" }}
        />
        <button className="btn btn-sm" onClick={handleSubscribe}>订阅</button>
        <button className="btn btn-sm ghost" onClick={() => { setSubscribedCode(""); setDeals([]); }}>全部</button>
        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-dim)" }}>
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} /> 自动滚动
        </label>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
          WS {status} · {subscribedCode ? `已订阅: ${subscribedCode}` : "查看全部成交流"}
        </span>
      </div>
      <div style={{ flex: 1, overflow: "auto", padding: "4px 12px" }} ref={listRef}>
        {deals.length === 0 && (
          <div style={{ padding: 24, color: "var(--text-dim)", textAlign: "center" }}>
            {status === "connected"
              ? (subscribedCode ? `暂无 ${subscribedCode} 成交数据（等待推送中）` : "暂无成交数据（等待推送中）")
              : `实时通道尚未连接（${status}），暂无法接收成交推送`}
          </div>
        )}
        <table className="data-table" style={{ width: "100%" }}>
          <thead>
            <tr><th>时间</th><th>代码</th><th>价格</th><th>数量</th><th>金额</th><th>方向</th><th>标志</th></tr>
          </thead>
          <tbody>
            {deals.map((d, i) => {
              const price = Number(d.price || 0);
              const volume = Number(d.volume || 0);
              const amount = price * volume;
              const isBig = amount >= BIG_DEAL_THRESHOLD;
              const rawDir = d.side || d.direction || d.type;
              const isBuy = rawDir === "buy" || rawDir === 1 || rawDir === "BUY";
              const isSell = rawDir === "sell" || rawDir === -1 || rawDir === "SELL";
              const dir = isBuy ? "买" : isSell ? "卖" : "中性";
              const ts = d.timestamp || d.ts || d.created_at || d.time || d._t;
              const timeStr = ts
                ? new Date(typeof ts === "number" && ts < 10000000000000 ? ts * 1000 : ts)
                    .toLocaleTimeString("zh-CN", { hour12: false })
                : "-";
              return (
                <tr key={d.deal_id || d.order_id || i} style={{ fontSize: 12 }}>
                  <td>{timeStr}</td>
                  <td className="code">{d.code || d.symbol || "-"}</td>
                  <td style={{ color: dir === "买" ? "var(--up)" : dir === "卖" ? "var(--down)" : "var(--text)" }}>{price ? price.toFixed(2) : "-"}</td>
                  <td>{volume ? volume.toLocaleString() : "-"}</td>
                  <td>{amount ? fmtAmount(amount) : (d.amount ?? "-")}</td>
                  <td><span style={{ color: dir === "买" ? "var(--up)" : dir === "卖" ? "var(--down)" : "var(--text-dim)" }}>{dir}</span></td>
                  <td>{isBig ? <span style={{ color: "var(--warn)", fontWeight: 600 }}>大单</span> : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
