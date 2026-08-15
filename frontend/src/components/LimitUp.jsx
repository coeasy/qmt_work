import { useEffect, useState } from "react";
import { api } from "../api.js";

// 涨停监控 / 打板助手：股票池 + 三因子触发（涨停价/时间窗/tick涨幅）+ 可选自动买入
export default function LimitUp() {
  const [st, setSt] = useState(null);
  const [code, setCode] = useState("");
  const [err, setErr] = useState("");
  const [params, setParams] = useState({
    limit_pct: 0.1, cutoff: "10:00", min_rise: 0.03,
    buy_volume: 0, do_trade: false, interval: 2,
  });

  async function load() {
    try { setSt(await api.limitupStatus()); } catch (e) { setErr(e.message); }
  }
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, []);

  async function add() {
    const c = code.trim();
    if (!c) return;
    try { await api.limitupPoolAdd(c); setCode(""); setErr(""); load(); }
    catch (e) { setErr(e.message); }
  }
  async function remove(c) {
    try { await api.limitupPoolRemove(c); load(); } catch (e) { setErr(e.message); }
  }
  async function start() {
    try { await api.limitupStart(params); setErr(""); load(); }
    catch (e) { setErr(e.message); }
  }
  async function stop() {
    try { await api.limitupStop(); load(); } catch (e) { setErr(e.message); }
  }
  async function resetTrig() {
    try { await api.limitupReset(); load(); } catch (e) { setErr(e.message); }
  }

  const pool = st?.pool || [];
  const events = st?.events || [];
  return (
    <div>
      <h2 className="page-title">涨停监控 · 打板助手</h2>
      <p className="page-sub">借鉴 QMT-QuantLimit：实时轮询真实行情，last≥涨停价 且 时间≤截止 且 近25 tick 涨幅达标即触发；可自动涨停价买入（走风控）</p>
      {err && <div className="toast err">{err}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3>股票池（{pool.length}）</h3>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="代码，如 600519.SH" value={code}
                   onChange={(e) => setCode(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && add()} />
            <button onClick={add}>添加</button>
          </div>
          {pool.length === 0 ? <p className="muted">暂无监控标的</p> : (
            <table>
              <thead><tr><th>代码</th><th>名称</th><th></th></tr></thead>
              <tbody>
                {pool.map((p) => (
                  <tr key={p.code}>
                    <td className="code">{p.code}</td><td>{p.name}</td>
                    <td><button onClick={() => remove(p.code)}>移除</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h3>触发参数与状态</h3>
          <div className="row">
            <label>涨停幅度</label>
            <input type="number" step="0.01" value={params.limit_pct}
                   onChange={(e) => setParams({ ...params, limit_pct: +e.target.value })} />
            <label>截止时间</label>
            <input type="text" value={params.cutoff}
                   onChange={(e) => setParams({ ...params, cutoff: e.target.value })} />
          </div>
          <div className="row">
            <label>tick涨幅</label>
            <input type="number" step="0.01" value={params.min_rise}
                   onChange={(e) => setParams({ ...params, min_rise: +e.target.value })} />
            <label>轮询秒</label>
            <input type="number" step="0.5" value={params.interval}
                   onChange={(e) => setParams({ ...params, interval: +e.target.value })} />
          </div>
          <div className="row">
            <label>买入股数</label>
            <input type="number" step="100" value={params.buy_volume}
                   onChange={(e) => setParams({ ...params, buy_volume: +e.target.value })} />
            <label><input type="checkbox" checked={params.do_trade}
                          onChange={(e) => setParams({ ...params, do_trade: e.target.checked })} /> 自动买入</label>
          </div>
          <div className="btn-row">
            <button onClick={start} disabled={st?.running}>启动监控</button>
            <button onClick={stop} disabled={!st?.running}>停止</button>
            <button onClick={resetTrig}>重置触发</button>
          </div>
          <p className="muted">
            状态：<span className={`tag ${st?.running ? "run" : "fail"}`}>{st?.running ? "监控中" : "已停止"}</span>
            {" "}已触发：<b>{st?.total_triggered ?? 0}</b>
            {st?.do_trade && <span className="tag warn">自动买入已开启</span>}
          </p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>触发事件（最新 50 条）</h3>
        {events.length === 0 ? <p className="muted">暂无触发事件</p> : (
          <table>
            <thead><tr><th>时间</th><th>代码</th><th>涨停价</th><th>触发价</th></tr></thead>
            <tbody>
              {events.slice().reverse().map((e, i) => (
                <tr key={i}>
                  <td>{e.ts}</td><td className="code">{e.code}</td>
                  <td>{e.limit}</td><td className="up">{e.price}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
