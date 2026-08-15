import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 模拟盘（P1）
   虚拟撮合，实时真实行情 mark-to-market。验证策略后再实盘。
*/

const TABS = [
  { key: "order",  label: "下单" },
  { key: "positions", label: "持仓" },
  { key: "trades", label: "成交" },
  { key: "account", label: "账户" },
  { key: "metrics", label: "指标" },
];

export default function Paper() {
  const [tab, setTab] = useState("positions");
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);

  /* ---- 下单表单 ---- */
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState("BUY");
  const [priceType, setPriceType] = useState("LIMIT");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");

  /* ---- 数据 ---- */
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [account, setAccount] = useState(null);
  const [metrics, setMetrics] = useState(null);

  const loadAll = () => {
    api.paperPositions().then(setPositions).catch(() => setPositions([]));
    api.paperTrades().then(setTrades).catch(() => setTrades([]));
    api.paperAccount().then(setAccount).catch(() => setAccount(null));
    api.paperMetrics().then(setMetrics).catch(() => setMetrics(null));
  };

  useEffect(() => { loadAll(); }, []);

  async function submitOrder() {
    setLoading(true); setMsg(null);
    try {
      if (!symbol) throw new Error("请输入股票代码");
      if (!qty || qty < 100) throw new Error("数量至少 100 股（1 手）");
      if (priceType === "LIMIT" && !price) throw new Error("限价单请输入价格");
      const body = { symbol, side, priceType, qty: +qty };
      if (priceType === "LIMIT") body.price = +price;
      await api.paperOrder(body);
      setMsg({ ok: true, t: `模拟 ${side === "BUY" ? "买入" : "卖出"} ${symbol} ${qty} 股 ${priceType} 已提交` });
      setSymbol(""); setQty(""); setPrice("");
      loadAll();
    } catch (e) {
      setMsg({ ok: false, t: e.message });
    } finally {
      setLoading(false);
    }
  }

  async function resetPaper() {
    setLoading(true); setMsg(null);
    try {
      await api.paperReset();
      setMsg({ ok: true, t: "模拟盘已重置" });
      loadAll();
    } catch (e) {
      setMsg({ ok: false, t: e.message });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>模拟盘 <span style={{ fontSize: 13, color: "#8aa0c4" }}>Paper Trading · 实时真实行情</span></h2>
        <p>虚拟撮合，用真实行情 mark-to-market，验证策略后再实盘</p>
      </div>

      <div className="card">
        <div className="btn-group">
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
          <button className="btn-danger" onClick={resetPaper} style={{ marginLeft: "auto" }}>
            重置模拟盘
          </button>
        </div>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      {tab === "order" && (
        <div className="card" style={{ maxWidth: 520 }}>
          <div className="form-row">
            <div className="form-field">
              <label>股票代码</label>
              <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="600519.SH" />
            </div>
            <div className="form-field">
              <label>方向</label>
              <div className="btn-group">
                <button className={side === "BUY" ? "active" : ""} onClick={() => setSide("BUY")}>买入</button>
                <button className={side === "SELL" ? "active" : ""} onClick={() => setSide("SELL")}>卖出</button>
              </div>
            </div>
            <div className="form-field">
              <label>委托类型</label>
              <div className="btn-group">
                <button className={priceType === "LIMIT" ? "active" : ""} onClick={() => setPriceType("LIMIT")}>限价</button>
                <button className={priceType === "MARKET" ? "active" : ""} onClick={() => setPriceType("MARKET")}>市价</button>
                <button className={priceType === "BEST" ? "active" : ""} onClick={() => setPriceType("BEST")}>最优</button>
              </div>
            </div>
            {priceType === "LIMIT" && (
              <div className="form-field">
                <label>价格</label>
                <input type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="0.00" />
              </div>
            )}
            <div className="form-field">
              <label>数量（股）</label>
              <input type="number" value={qty} onChange={(e) => setQty(e.target.value)} placeholder="100" />
            </div>
          </div>
          <button className="btn-primary" onClick={submitOrder} disabled={loading}>
            {loading ? "提交中…" : `模拟 ${side === "BUY" ? "买入" : "卖出"} ${symbol || "..."}`}
          </button>
        </div>
      )}

      {tab === "positions" && (
        <DataTable positions={positions} />
      )}

      {tab === "trades" && (
        <DataTable trades={trades} />
      )}

      {tab === "account" && (
        <div className="card">
          {account ? <KVList data={account} /> : <Empty>暂无账户数据</Empty>}
        </div>
      )}

      {tab === "metrics" && (
        <div className="card">
          {metrics ? <KVList data={metrics} /> : <Empty>暂无指标数据</Empty>}
        </div>
      )}
    </div>
  );
}

function DataTable({ positions, trades }) {
  const rows = positions || trades || [];
  if (!rows.length) return <Empty>暂无数据</Empty>;
  const first = rows[0];
  const headers = Object.keys(first);
  return (
    <div className="card">
      <div style={{ overflowX: "auto" }}>
        <table className="table">
          <thead><tr>{headers.map((h) => <th key={h}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {headers.map((h) => (
                  <td key={h}>
                    {typeof r[h] === "number"
                      ? <span>{Number(r[h]).toFixed(4)}</span>
                      : <span>{String(r[h] ?? "")}</span>}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function KVList({ data }) {
  const entries = Object.entries(data).filter(([k]) => k !== "error");
  return (
    <table className="table">
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td style={{ width: 160, color: "#8aa0c4" }}>{k}</td>
            <td><strong>{typeof v === "number" ? v.toFixed(2) : String(v)}</strong></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "32px 24px", textAlign: "center", color: "#556" }}>{children}</div>;
}