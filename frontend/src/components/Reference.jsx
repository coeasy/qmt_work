import { useEffect, useState } from "react";
import { api } from "../api.js";

// 参考数据：交易日历 / 板块列表 / 板块成分 / 财务摘要 / L2 逐笔（借鉴 quant-qmt-proxy 参考数据能力）
export default function Reference() {
  const [err, setErr] = useState("");
  const [tab, setTab] = useState("calendar");
  const [calendar, setCalendar] = useState([]);
  const [sectors, setSectors] = useState([]);
  const [stocks, setStocks] = useState([]);
  const [fin, setFin] = useState(null);
  const [l2, setL2] = useState([]);
  const [code, setCode] = useState("600519.SH");
  const [sector, setSector] = useState("");

  useEffect(() => { loadSectors(); loadCalendar(); }, []);
  useEffect(() => { if (sector) loadStocks(); }, [sector]);

  async function wrap(fn) {
    try { setErr(""); return await fn(); }
    catch (e) { setErr(e.message); return null; }
  }
  async function loadCalendar() {
    const d = await wrap(() => api.calendar("", ""));
    if (d) setCalendar(d);
  }
  async function loadSectors() {
    const d = await wrap(() => api.sectors());
    if (d) { setSectors(d); if (!sector && d.length) setSector(d.find((s) => s.includes("沪深A股")) || d[0]); }
  }
  async function loadStocks() {
    const d = await wrap(() => api.sectorStocks(sector));
    if (d) setStocks(d);
  }
  async function queryFin() {
    const d = await wrap(() => api.financial(code));
    if (d) setFin(d);
  }
  async function queryL2() {
    const d = await wrap(() => api.l2(code, 100));
    if (d) setL2(d);
  }

  const FIN_LABEL = { EPS: "每股收益", BPS: "每股净资产", OPERATE_INCOME: "营业收入",
    TOTAL_OPERATE_INCOME: "总营业收入", PARENT_NETPROFIT: "归母净利润",
    TOTAL_OPERATE_EXPENSE: "营业总支出", ROE: "净资产收益率", CAPITAL: "总股本",
    TOTAL_OPERATE_INCOME_YOY: "营收同比", PARENT_NETPROFIT_YOY: "净利同比" };

  return (
    <div>
      <h2 className="page-title">参考数据</h2>
      <p className="page-sub">交易日历 / 板块成分 / 财务摘要 / Level-2 逐笔（真实数据，需已连接券商）</p>
      {err && <div className="toast err">{err}</div>}

      <div className="row" style={{ marginBottom: 12 }}>
        {[["calendar", "交易日历"], ["sectors", "板块成分"], ["financial", "财务摘要"], ["l2", "L2 逐笔"]]
          .map(([id, label]) => (
            <button key={id} onClick={() => setTab(id)}
                    style={tab === id ? { background: "var(--accent)", color: "#fff" } : {}}>{label}</button>
          ))}
      </div>

      {tab === "calendar" && (
        <div className="card">
          <h3>交易日历（最近一批）</h3>
          {calendar.length === 0 ? <p className="muted">无数据</p> : (
            <table>
              <thead><tr><th>交易日</th></tr></thead>
              <tbody>{calendar.slice(0, 60).map((d) => <tr key={d}><td className="code">{d}</td></tr>)}</tbody>
            </table>
          )}
        </div>
      )}

      {tab === "sectors" && (
        <div className="grid grid-2">
          <div className="card">
            <h3>板块列表（{sectors.length}）</h3>
            <table>
              <thead><tr><th>板块</th></tr></thead>
              <tbody>
                {sectors.map((s) => (
                  <tr key={s}><td><span className="link" onClick={() => setSector(s)}>{s}</span></td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card">
            <h3>成分股：{sector || "（未选择）"}</h3>
            {stocks.length === 0 ? <p className="muted">点击左侧板块查看成分</p> : (
              <table>
                <thead><tr><th>#</th><th>代码</th></tr></thead>
                <tbody>{stocks.slice(0, 200).map((c, i) =>
                  <tr key={c}><td>{i + 1}</td><td className="code">{c}</td></tr>)}</tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {tab === "financial" && (
        <div className="card">
          <h3>财务摘要</h3>
          <div className="row">
            <input placeholder="代码" value={code} onChange={(e) => setCode(e.target.value)}
                   style={{ width: 150 }} />
            <button onClick={queryFin}>查询</button>
          </div>
          {fin && (
            <table>
              <thead><tr><th>指标</th><th>值</th></tr></thead>
              <tbody>
                <tr><td>报告期</td><td>{fin.report_time}</td></tr>
                {Object.entries(FIN_LABEL).map(([k, label]) => (
                  <tr key={k}><td>{label}</td><td>{fin[k] ?? "-"}</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === "l2" && (
        <div className="card">
          <h3>Level-2 逐笔成交</h3>
          <div className="row">
            <input placeholder="代码" value={code} onChange={(e) => setCode(e.target.value)}
                   style={{ width: 150 }} />
            <button onClick={queryL2}>查询</button>
          </div>
          {l2.length === 0 ? <p className="muted">无数据（需 L2 权限）</p> : (
            <table>
              <thead><tr><th>时间</th><th>价格</th><th>量</th><th>方向</th></tr></thead>
              <tbody>
                {l2.map((t, i) => (
                  <tr key={i}>
                    <td>{t.time}</td><td>{t.price}</td><td>{t.volume}</td>
                    <td className={t.type === "buy" ? "up" : "down"}>
                      {t.type === "buy" ? "主买" : "主卖"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
