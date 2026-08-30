// 报价牌 / 综合排名（通达信式核心窗口）：板块、自选股、综合排名；
// 可排序表格 + 实时刷新（REST 批量快照首屏 + QuoteHub 全局单连接 WS 增量）。
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { navToQuote } from "../lib/nav.js";
import { useQuotes } from "../lib/quoteHub.jsx";
// 金额格式化统一走 lib/format.js（原本地副本已删除，避免第 3 份口径漂移）
import { fmtAmount } from "../lib/format.js";

const WATCH_KEY = "qmt_work.watchlist.v1";

// 列定义：k 用于排序取值；num 为数值列；kind 决定渲染样式
//   - pct：整格红/绿底白字（TDX 经典涨跌幅格）+ 箭头
//   - price/amount：方向着色字
//   - src：数据源小徽标
const COLS = [
  { k: "code", label: "代码", num: false, kind: "code" },
  { k: "name", label: "名称", num: false, kind: "name" },
  { k: "last", label: "最新", num: true, kind: "price",
    fmt: (v) => (v == null ? "—" : Number(v).toFixed(2)) },
  { k: "pct", label: "涨跌幅", num: true, kind: "pct",
    fmt: (v) => (v == null ? "—" : (v >= 0 ? "▲ " : "▼ ") + (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%") },
  { k: "chg", label: "涨跌额", num: true, kind: "price",
    fmt: (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(2)) },
  { k: "amp", label: "振幅%", num: true, kind: "plain",
    fmt: (v) => (v == null ? "—" : Number(v).toFixed(2)) },
  { k: "amount", label: "成交额", num: true, kind: "plain",
    fmt: (v) => (v == null ? "—" : fmtAmount(v)) },
  { k: "volume", label: "成交量", num: true, kind: "plain",
    fmt: (v) => (v == null ? "—" : fmtAmount(v)) },
  { k: "source", label: "源", num: false, kind: "src", fmt: (v) => v || "—" },
];

const SRC_SHORT = { eltdx: "TDX", broker: "券商", cache: "缓存" };
function cellClass(c, v) {
  if (c.kind === "price" || c.kind === "pct") {
    if (v == null) return "";
    return v >= 0 ? "up" : "down";
  }
  return "";
}
function cellHtmlClass(c, v) {
  // 涨跌幅格：自身带方向底色（与字体红绿相反，DOM 类不冲突，用专用类）
  if (c.kind === "pct") return v == null ? "" : (v >= 0 ? "qb-pct up" : "qb-pct down");
  return "";
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

  // 批量快照首屏 + QuoteHub 全局单连接增量（替代自建裸 WS：无重连、绕过多路复用的历史问题）
  const { quotes } = useQuotes(codes);
  useEffect(() => {
    if (!codes.length) { setRows({}); return; }
    let stopped = false;
    api.marketQuotes({ codes }).then((r) => {
      if (stopped) return;
      const m = {};
      (r.items || []).forEach((q) => { m[q.code] = derive(q); });
      setRows(m);
    }).catch(() => {});
    return () => { stopped = true; };
  }, [codes]);

  // WS tick → 就地更新对应行（快照里没有的 code 忽略）
  useEffect(() => {
    setRows((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const c of codes) {
        const q = quotes[c];
        if (q) { next[c] = derive(q); changed = true; }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quotes]);

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
    // v3：nav 协议带参直达（多实例行情 tab 各看各的票，不再写 sessionStorage）
    navToQuote(code);
  };

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
                  c.kind === "src" ? (
                    <td key={c.k} className="qb-src">
                      <span className={`qb-src-tag ${r[c.k] ? `src-${r[c.k]}` : ""}`}>
                        {SRC_SHORT[r[c.k]] || "—"}
                      </span>
                    </td>
                  ) : (
                    <td key={c.k}
                      className={`${c.num ? "num" : ""} ${cellClass(c, r[c.k])} ${cellHtmlClass(c, r[c.k])}`}>
                      {c.fmt ? c.fmt(r[c.k]) : r[c.k]}
                    </td>
                  )
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
