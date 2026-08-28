// 指数跑马灯（通达信顶栏右侧）：订阅核心指数，滚动展示 代码/最新/涨跌幅。
import { useEffect, useRef, useState } from "react";

// 核心指数：上证指数 / 深证成指 / 创业板指 / 沪深300
const INDEX_CODES = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"];
const INDEX_NAMES = {
  "000001.SH": "上证", "399001.SZ": "深证", "399006.SZ": "创业", "000300.SH": "沪深300",
};

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

export default function Ticker() {
  const [quotes, setQuotes] = useState({});
  const wsRef = useRef(null);
  const timerRef = useRef(null);

  useEffect(() => {
    let stopped = false;
    function connect() {
      if (stopped) return;
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        try { ws.send(JSON.stringify({ action: "subscribe", codes: INDEX_CODES })); } catch { /* noop */ }
      };
      ws.onmessage = (e) => {
        let msg; try { msg = JSON.parse(e.data); } catch { return; }
        const take = (it) => {
          if (it && INDEX_CODES.includes(it.code)) {
            setQuotes((prev) => ({ ...prev, [it.code]: it }));
          }
        };
        if (msg.type === "quotes" && Array.isArray(msg.data?.items)) msg.data.items.forEach(take);
        else if (msg.type === "quotes_replay" && Array.isArray(msg.data?.items)) msg.data.items.forEach(take);
        else if (msg.type === "quote") take(msg.data);
        else if (msg.type === "snapshot" && msg.quotes) INDEX_CODES.forEach((c) => { if (msg.quotes[c]) take(msg.quotes[c]); });
      };
      ws.onclose = () => {
        if (stopped) return;
        timerRef.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
    }
    connect();
    return () => {
      stopped = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } wsRef.current = null; }
    };
  }, []);

  // 跑马灯：内容复制为两份，无缝循环滚动（CSS animation 实现 -50% 位移）
  const renderItems = () => INDEX_CODES.map((c) => {
    const q = quotes[c];
    const last = q?.last != null ? Number(q.last) : null;
    const pre = q?.preClose != null ? Number(q.preClose) : (q?.lastClose != null ? Number(q.lastClose) : null);
    const pct = last != null && pre ? ((last - pre) / pre) * 100 : null;
    const cls = pct == null ? "" : pct >= 0 ? "up" : "down";
    return (
      <span className="ticker-item" key={c}>
        <span className="ticker-name">{INDEX_NAMES[c]}</span>
        <span className="ticker-val">{last != null ? last.toFixed(2) : "—"}</span>
        <span className={cls}>{pct != null ? (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%" : "—"}</span>
      </span>
    );
  });

  return (
    <div className="ticker" aria-label="指数行情">
      <div className="ticker-track">
        <div className="ticker-group">{renderItems()}</div>
        <div className="ticker-group" aria-hidden="true">{renderItems()}</div>
      </div>
    </div>
  );
}
