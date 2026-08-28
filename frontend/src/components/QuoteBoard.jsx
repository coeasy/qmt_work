// 报价牌 / 综合排名（通达信式核心窗口）：板块、自选股、综合排名；
// 可排序表格 + 实时刷新（从服务端缓存/批量快照拉取，WS 增量更新）。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";

const WATCH_KEY = "qmt_work.watchlist.v1";

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

// 列定义：k 用于排序取值；num 为数值列
const COLS = [
  { k: "code", label: "代码", num: false },
  { k: "name", label: "名称", num: false },
  { k: "last", label: "最新", num: true, fmt: (v) => (v == null ? "—" : Number(v).toFixed(2)) },
  { k: "pct", label: "涨跌幅%", num: true, cls: (v) => (v == null ? "" : v >= 0 ? "up" : "down"),
    fmt: (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2)) },
  { k: "chg", label: "涨跌额", num: true, cls: (v) => (v == null ? "" : v >= 0 ? "up" : "down"),
    fmt: (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(2)) },
  { k: "amp", label: "振幅%", num: true, fmt: (v) => (v == null ? "—" : Number(v).toFixed(2)) },
  { k: "amount", label: "成交额", num: true, fmt: (v) => (v == null ? "—" : fmtAmount(v)) },
  { k: "volume", label: "成交量", num: true, fmt: (v) => (v == null ? "—" : fmtAmount(v)) },
  { k: "source", label: "源", num: false, fmt: (v) => (v || "—") },
];

function fmtAmount(n) {
  if (n == null) return "—";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(2) + "万";
  return String(Math.round(n));
}

function derive(q) {
  const last = q.last != null ? Number(q.last) : null;
  const pre = q.preClose != null ? Number(q.preClose) : (q.lastClose != null ? Number(q.lastClose) : (q.open != null ? Number(q.open) : null));
  const high = q.high != null ? Number(q.high) : null;
  const low = q.low != null ? Number(q.low) : null;
  const chg = last != null && pre ? last - pre : null;
  const pct = chg != null && pre ? (chg / pre) * 100 : null;
  const amp = high != null && low != null && pre ? ((high - low) / pre) * 100 : null;
  return {
    code: q.code, name: q.name || "", last, chg, pct, amp,
    amount: q.amount != null ? Number(q.amount) : null,
    volume: q.volume != null ? Number(q.volume) : null,
    source: q.source || "",
  };
}

export default function QuoteBoard() {
  const [board, setBoard] = useState("sector"); // sector | watch | rank
  const [sector, setSector] = useState(""); // 板块/综合排名 选中的板块
  const [sectors, setSectors] = useState([]);
  const [codes, setCodes] = useState([]);
  const [rows, setRows] = useState({}); // code -> derived
  const [sort, setSort] = useState({ k: "pct", dir: -1 });
  const [loading, setLoading] = useState(false);
  const wsRef = useRef(null);

  // F6：外部事件切到指定视图
  useEffect(() => {
    const onSwitch = (e) => {
      const k = e.detail;
      if (k === "watch" || k === "sector" || k === "rank") setBoard(k);
    };
    window.addEventListener("qb:switch", onSwitch);
    return () => window.removeEventListener("qb:switch", onSwitch);
  }, []);

  // 板块列表
  useEffect(() => {
    api.sectors().then((list) => {
      setSectors(list || []);
      if (!sector && list && list.length) setSector(list[0].code || list[0].name);
    }).catch(() => setSectors([]));
  }, []);

  // 加载代码列表
  useEffect(() => {
    setLoading(true);
    let canceled = false;
    (async () => {
      try {
        let list = [];
        if (board === "watch") {
          try { list = JSON.parse(localStorage.getItem(WATCH_KEY) || "[]"); } catch { list = []; }
        } else {
          const sec = sector || (sectors[0] && (sectors[0].code || sectors[0].name));
          if (sec) {
            const stk = await api.sectorStocks(sec);
            list = (stk || []).map((s) => s.code || s.stock_code || s).filter(Boolean).slice(0, 300);
          }
        }
        if (!canceled) setCodes(list);
      } finally { if (!canceled) setLoading(false); }
    })();
    return () => { canceled = true; };
  }, [board, sector, sectors]);

  // 批量快照 + WS 增量
  useEffect(() => {
    if (!codes.length) { setRows({}); return; }
    let stopped = false;
    api.marketQuotes({ codes }).then((r) => {
      if (stopped) return;
      const m = {};
      (r.items || []).forEach((q) => { m[q.code] = derive(q); });
      setRows(m);
    }).catch(() => {});

    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => { try { ws.send(JSON.stringify({ action: "subscribe", codes })); } catch {} };
    ws.onmessage = (e) => {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      const take = (it) => {
        if (it && codes.includes(it.code)) setRows((prev) => ({ ...prev, [it.code]: derive(it) }));
      };
      if (msg.type === "quotes" && Array.isArray(msg.data?.items)) msg.data.items.forEach(take);
      else if (msg.type === "quotes_replay" && Array.isArray(msg.data?.items)) msg.data.items.forEach(take);
      else if (msg.type === "quote") take(msg.data);
    };
    ws.onerror = () => { try { ws.close(); } catch {} };
    return () => { stopped = true; try { ws.close(); } catch {} wsRef.current = null; };
  }, [codes]);

  const sorted = useMemo(() => {
    const arr = Object.values(rows);
    const { k, dir } = sort;
    arr.sort((a, b) => {
      const va = a[k], vb = b[k];
      if (va == null) return 1; if (vb == null) return -1;
      if (typeof va === "string") return va.localeCompare(vb) * dir;
      return (va - vb) * dir;
    });
    return arr;
  }, [rows, sort]);

  const setSortKey = (k) => setSort((s) => (s.k === k ? { k, dir: -s.dir } : { k, dir: k === "code" || k === "name" ? 1 : -1 }));

  const openStock = (code) => {
    try { sessionStorage.setItem("qmt_work.code", code); } catch {}
    window.dispatchEvent(new CustomEvent("nav", { detail: "stock" }));
  };

  const RANK_CHIPS = [
    { k: "pct", label: "涨幅榜" }, { k: "pct", dir: 1, label: "跌幅榜" },
    { k: "amp", label: "振幅榜" }, { k: "amount", label: "成交额榜" },
    { k: "volume", label: "成交量榜" },
  ];

  // 综合排名：TDX 80 风格——3 列并排（涨幅 / 振幅 / 成交额），各取 top N
  const RANK_COLS = [
    { k: "pct", dir: -1, title: "涨幅榜" },
    { k: "amp", dir: -1, title: "振幅榜" },
    { k: "amount", dir: -1, title: "成交额榜" },
  ];
  const RANK_TOP = 15;
  const rankGroups = RANK_COLS.map((c) => {
    const arr = [...sorted].sort((a, b) => {
      const va = a[c.k], vb = b[c.k];
      if (va == null) return 1; if (vb == null) return -1;
      return (va - vb) * c.dir;
    });
    return { ...c, rows: arr.filter((r) => r[c.k] != null).slice(0, RANK_TOP) };
  });

  return (
    <div className="page quote-board">
      <div className="qb-header">
        <div className="qb-board-tabs">
          <span className={`qb-board ${board === "sector" ? "active" : ""}`} onClick={() => setBoard("sector")}>板块</span>
          <span className={`qb-board ${board === "watch" ? "active" : ""}`} onClick={() => setBoard("watch")}>自选股</span>
          <span className={`qb-board ${board === "rank" ? "active" : ""}`} onClick={() => setBoard("rank")}>综合排名</span>
        </div>
        {(board === "sector" || board === "rank") && (
          <select className="qb-sector" value={sector} onChange={(e) => setSector(e.target.value)}>
            {sectors.map((s, i) => (
              <option key={i} value={s.code || s.name}>{s.name || s.code}</option>
            ))}
          </select>
        )}
        <span className="qb-count">{codes.length} 只 · {loading ? "加载中…" : "实时"}</span>
      </div>

      {board === "rank" && (
        <div className="qb-rank-chips">
          {RANK_CHIPS.map((c, i) => (
            <span key={i} className={`qb-chip ${sort.k === c.k && (c.dir ? sort.dir === c.dir : true) ? "active" : ""}`}
              onClick={() => setSort({ k: c.k, dir: c.dir || -1 })}>{c.label}</span>
          ))}
        </div>
      )}

      {board === "rank" && (
        <div className="qb-rank-cols">
          {rankGroups.map((g) => (
            <div className="qb-rank-col" key={g.k}>
              <div className="qb-rank-col-title">{g.title}</div>
              <table className="qb-table qb-rank-table">
                <thead>
                  <tr><th className="num">#</th><th>代码</th><th>名称</th><th className="num">最新</th><th className="num">{g.k === "pct" ? "涨幅" : g.k === "amp" ? "振幅" : "成交额"}</th></tr>
                </thead>
                <tbody>
                  {g.rows.length === 0 ? (
                    <tr><td colSpan={5} className="muted" style={{ textAlign: "center", padding: 8 }}>无数据</td></tr>
                  ) : g.rows.map((r, i) => (
                    <tr key={r.code} onClick={() => openStock(r.code)}>
                      <td className="num">{i + 1}</td>
                      <td className="code">{r.code}</td>
                      <td>{r.name || ""}</td>
                      <td className="num">{r.last != null ? Number(r.last).toFixed(2) : "—"}</td>
                      <td className={`num ${r[g.k] == null ? "" : r[g.k] >= 0 ? "up" : "down"}`}>
                        {g.k === "amount" ? fmtAmount(r[g.k]) : (r[g.k] != null ? (r[g.k] >= 0 ? "+" : "") + Number(r[g.k]).toFixed(2) : "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      {board !== "rank" && (
      <div className="qb-table-wrap">
        <table className="qb-table">
          <thead>
            <tr>
              {COLS.map((c) => (
                <th key={c.k} className={c.num ? "num" : ""} onClick={() => setSortKey(c.k)}>
                  {c.label}{sort.k === c.k ? (sort.dir < 0 ? " ▼" : " ▲") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr><td colSpan={COLS.length} className="muted" style={{ textAlign: "center", padding: 20 }}>
                {board === "watch" ? "自选股为空，可在底部「自选股」面板添加" : "无数据"}
              </td></tr>
            ) : sorted.map((r) => (
              <tr key={r.code} onClick={() => openStock(r.code)}>
                {COLS.map((c) => (
                  <td key={c.k} className={`${c.num ? "num" : ""} ${c.cls ? c.cls(r[c.k]) : ""}`}>{c.fmt ? c.fmt(r[c.k]) : r[c.k]}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}
    </div>
  );
}
