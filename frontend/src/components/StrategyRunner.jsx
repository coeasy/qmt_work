import { useEffect, useState } from "react";
import { api } from "../api.js";

// 策略运行容器（P0）：在平台内把生成的策略当作实盘/模拟机器人运行。
// 复用与模板生成一致的信号逻辑（ma_cross/macd/rsi/limitup），真实行情 + 真实/模拟下单。

const RTYPES = [
  { id: "ma_cross", label: "双均线金叉/死叉" },
  { id: "macd", label: "MACD 金叉/死叉" },
  { id: "rsi", label: "RSI 超买超卖" },
  { id: "limitup", label: "涨停监控打板" },
];

const DEFAULT_PARAMS = {
  ma_cross: { fast: 5, slow: 20, volume: 100 },
  macd: { fast: 12, slow: 26, signal: 9, volume: 100 },
  rsi: { period: 14, buy_at: 30, sell_at: 70, volume: 100 },
  limitup: { limit_pct: 0.1, cutoff: "10:00", buy_volume: 100 },
};

function ParamFields({ rtype, params, setParams }) {
  const set = (k, v) => setParams((p) => ({ ...p, [k]: v }));
  if (rtype === "ma_cross")
    return (
      <div className="row">
        <label style={{ width: 70 }}>快线</label>
        <input style={{ width: 90 }} value={params.fast ?? 5}
               onChange={(e) => set("fast", e.target.value)} />
        <label style={{ width: 70 }}>慢线</label>
        <input style={{ width: 90 }} value={params.slow ?? 20}
               onChange={(e) => set("slow", e.target.value)} />
      </div>
    );
  if (rtype === "macd")
    return (
      <div className="row">
        <label style={{ width: 70 }}>快线</label>
        <input style={{ width: 80 }} value={params.fast ?? 12}
               onChange={(e) => set("fast", e.target.value)} />
        <label style={{ width: 70 }}>慢线</label>
        <input style={{ width: 80 }} value={params.slow ?? 26}
               onChange={(e) => set("slow", e.target.value)} />
        <label style={{ width: 70 }}>信号</label>
        <input style={{ width: 80 }} value={params.signal ?? 9}
               onChange={(e) => set("signal", e.target.value)} />
      </div>
    );
  if (rtype === "rsi")
    return (
      <div className="row">
        <label style={{ width: 70 }}>周期</label>
        <input style={{ width: 80 }} value={params.period ?? 14}
               onChange={(e) => set("period", e.target.value)} />
        <label style={{ width: 80 }}>买入阈值</label>
        <input style={{ width: 80 }} value={params.buy_at ?? 30}
               onChange={(e) => set("buy_at", e.target.value)} />
        <label style={{ width: 80 }}>卖出阈值</label>
        <input style={{ width: 80 }} value={params.sell_at ?? 70}
               onChange={(e) => set("sell_at", e.target.value)} />
      </div>
    );
  if (rtype === "limitup")
    return (
      <div className="row">
        <label style={{ width: 80 }}>涨停幅度</label>
        <input style={{ width: 90 }} value={params.limit_pct ?? 0.1}
               onChange={(e) => set("limit_pct", e.target.value)} />
        <label style={{ width: 80 }}>截止时间</label>
        <input style={{ width: 90 }} value={params.cutoff ?? "10:00"}
               onChange={(e) => set("cutoff", e.target.value)} />
      </div>
    );
  return null;
}

export default function StrategyRunner({ prefill }) {
  const [rtype, setRtype] = useState(prefill?.strategy_type || "ma_cross");
  const [codes, setCodes] = useState(prefill?.code || "600519.SH");
  const [mode, setMode] = useState("paper");
  const [name, setName] = useState("");
  const [interval, setInterval] = useState(60);
  const [params, setParams] = useState(
    prefill?.params || DEFAULT_PARAMS[prefill?.strategy_type || "ma_cross"]);
  const [runs, setRuns] = useState([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [logs, setLogs] = useState({});       // run_id -> logs[]
  const [openLog, setOpenLog] = useState(null);
  const [precheck, setPrecheck] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loadId, setLoadId] = useState("");
  const [loaded, setLoaded] = useState(null);
  const [loadErr, setLoadErr] = useState("");

  async function loadById() {
    if (!loadId.trim()) return;
    setLoadErr(""); setLoaded(null);
    try {
      const r = await api.strategyRunGet(Number(loadId.trim()));
      setLoaded(r);
      // 预填表单：类型/标的/模式/间隔
      if (r.strategy_type) setRtype(r.strategy_type);
      if (r.codes?.length) setCodes(r.codes.join(","));
      if (r.mode) setMode(r.mode);
      if (r.interval_seconds) setInterval(r.interval_seconds);
      if (r.params) setParams({ ...DEFAULT_PARAMS[r.strategy_type] || {}, ...r.params });
      setMsg(`已加载实例 #${r.id}（${r.name || ""}）`);
    } catch (e) { setLoadErr(e.message); }
  }

  async function refresh() {
    try { setRuns(await api.strategyRunList()); } catch (e) { setErr(e.message); }
  }
  useEffect(() => { refresh(); /* eslint-disable */ }, []);

  // 应用「一键运行」预设
  useEffect(() => {
    if (prefill?.strategy_type) {
      setRtype(prefill.strategy_type);
      setParams(prefill.params || DEFAULT_PARAMS[prefill.strategy_type]);
      if (prefill.code) setCodes(prefill.code);
    }
    /* eslint-disable */
  }, [prefill]);

  async function create() {
    setErr(""); setMsg(""); setBusy(true);
    const codesArr = codes.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const volume = params.volume ?? params.buy_volume ?? 100;
    const body = {
      name: name || `${rtype}-${codesArr[0]}`,
      strategy_type: rtype,
      codes: codesArr,
      params: { ...params, volume },
      mode,
      interval_seconds: Number(interval) || 60,
    };
    try {
      const r = await api.strategyRunCreate(body);
      setMsg(`已创建实例 #${r.id}（${mode === "paper" ? "模拟盘" : "实盘"}）`);
      setName("");
      await refresh();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function start(id) {
    setErr("");
    try { await api.strategyRunStart(id); await refresh(); }
    catch (e) { setErr(e.message); }
  }
  async function stop(id) {
    setErr("");
    try { await api.strategyRunStop(id); await refresh(); }
    catch (e) { setErr(e.message); }
  }
  async function del(id) {
    setErr("");
    try { await api.strategyRunDelete(id); await refresh(); }
    catch (e) { setErr(e.message); }
  }
  async function toggleLogs(id) {
    if (openLog === id) { setOpenLog(null); return; }
    setOpenLog(id);
    try {
      const ls = await api.strategyRunLogs(id, 50);
      setLogs((m) => ({ ...m, [id]: ls }));
    } catch (e) { setErr(e.message); }
  }
  async function doPrecheck() {
    setPrecheck(null);
    const codesArr = codes.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    try {
      const r = await api.strategyRunPrecheck({
        code: codesArr[0] || "", direction: "buy",
        price: 0, volume: Number(params.volume ?? params.buy_volume ?? 100),
      });
      setPrecheck(r);
    } catch (e) { setErr(e.message); }
  }

  return (
    <div>
      <h2 className="page-title">策略运行容器</h2>
      <p className="page-sub">
        在平台进程内把策略当作机器人运行：复用模板信号逻辑，接入<strong>真实行情</strong>，
        实盘模式经<strong>风控闸门</strong>后真实下单、模拟模式走模拟盘引擎。无需把代码写盘交给 QMT 客户端另起进程。
      </p>
      {err && <div className="toast err">{err}</div>}
      {msg && <div className="toast ok">{msg}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3>新建运行实例</h3>
          <div className="row">
            <select value={rtype} onChange={(e) => {
              setRtype(e.target.value);
              setParams(DEFAULT_PARAMS[e.target.value]);
            }}>
              {RTYPES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
            <input placeholder="实例名称（可选）" value={name}
                   onChange={(e) => setName(e.target.value)} style={{ flex: 1 }} />
          </div>
          <div className="row">
            <label style={{ width: 70 }}>标的</label>
            <input style={{ flex: 1 }} value={codes}
                   onChange={(e) => setCodes(e.target.value)}
                   placeholder="600519.SH 或 多标的逗号分隔" />
          </div>
          <div className="row">
            <label style={{ width: 70 }}>模式</label>
            <select value={mode} onChange={(e) => setMode(e.target.value)} style={{ width: 130 }}>
              <option value="paper">模拟盘（安全）</option>
              <option value="live">实盘（真实下单）</option>
            </select>
            <label style={{ width: 90 }}>轮询间隔(s)</label>
            <input style={{ width: 90 }} value={interval}
                   onChange={(e) => setInterval(e.target.value)} />
          </div>
          <ParamFields rtype={rtype} params={params} setParams={setParams} />
          <div className="btn-row">
            <button className="btn-primary" disabled={busy} onClick={create}>创建实例</button>
            <button onClick={doPrecheck}>风控预检</button>
            {precheck && (
              <span className={`tag ${precheck.allowed ? "ok" : "fail"}`}>
                {precheck.allowed ? "预检通过" : "预检拦截"} · {precheck.reason}
              </span>
            )}
          </div>
        </div>

        <div className="card">
          <h3>运行实例（{runs.length}）</h3>
          <div className="row" style={{ marginBottom: 8 }}>
            <label style={{ width: 90 }}>按 ID 加载</label>
            <input style={{ width: 110 }} placeholder="实例 ID"
                   value={loadId} onChange={(e) => setLoadId(e.target.value)} />
            <button onClick={loadById}>加载并预填</button>
            {loadErr && <span className="tag fail">{loadErr}</span>}
          </div>
          {loaded && (
            <div className="card" style={{ marginBottom: 8, background: "var(--panel-2)" }}>
              <h3>实例 #{loaded.id} 详情</h3>
              <div style={{ fontSize: 12, lineHeight: 1.8 }}>
                <div>名称：{loaded.name} · 类型：{loaded.strategy_type} · 模式：{loaded.mode === "live" ? "实盘" : "模拟"}</div>
                <div>状态：<span className={`tag ${loaded.running ? "run" : ""}`}>{loaded.running ? "运行中" : loaded.status}</span> · 信号：{loaded.last_signal || "—"}</div>
              </div>
              <details>
                <summary style={{ cursor: "pointer", fontSize: 12, color: "#8aa0c4" }}>完整参数</summary>
                <pre style={{ fontSize: 11, maxHeight: 180, overflow: "auto" }}>{JSON.stringify(loaded, null, 2)}</pre>
              </details>
            </div>
          )}
          {runs.length === 0 && <p className="muted">暂无实例，请在左侧创建。</p>}
          <div className="table" style={{ marginTop: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th>ID</th><th>名称</th><th>类型</th><th>模式</th>
                  <th>状态</th><th>信号</th><th>动作</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td>{r.id}</td>
                    <td>{r.name}</td>
                    <td>{r.strategy_type}</td>
                    <td><span className="tag">{r.mode === "live" ? "实盘" : "模拟"}</span></td>
                    <td>
                      {r.running
                        ? <span className="tag run">运行中</span>
                        : <span className="tag">{r.status}</span>}
                    </td>
                    <td>{r.last_signal || "—"}</td>
                    <td style={{ fontSize: 12 }}>{r.last_action || "—"}</td>
                    <td>
                      <div className="btn-group">
                        {r.running
                          ? <button className="btn-sm" onClick={() => stop(r.id)}>停止</button>
                          : <button className="btn-sm btn-primary" onClick={() => start(r.id)}>启动</button>}
                        <button className="btn-sm" onClick={() => toggleLogs(r.id)}>日志</button>
                        <button className="btn-danger-sm" onClick={() => del(r.id)}>删除</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {openLog != null && logs[openLog] && (
            <div className="card" style={{ marginTop: 10, background: "var(--panel-2)" }}>
              <h3>运行日志 #{openLog}</h3>
              <div style={{ maxHeight: 220, overflow: "auto", fontSize: 12, fontFamily: "monospace" }}>
                {logs[openLog].length === 0 && <p className="muted">暂无日志（启动后开始产生）。</p>}
                {logs[openLog].map((l, i) => (
                  <div key={i}>
                    <span className="muted">[{l.ts}]</span>{" "}
                    <span className={`tag ${l.level === "error" ? "fail" : l.level === "reject" ? "warn" : ""}`}>
                      {l.level}
                    </span>{" "}
                    {l.message}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
