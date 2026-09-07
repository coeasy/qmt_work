import {useState, useEffect} from "react";
import { api } from "../api.js";
import ConfirmTradeModal from "./ui/ConfirmTradeModal.jsx";
import { useServerEvents } from "../hooks/useSystemWS.js";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
import { orderStatus } from "../lib/format.js";

// 算法单（TWAP/VWAP 时间拆单）：借鉴 Rockyzsu/QMT 算法单能力
export default function Algo() {
  const [jobs, setJobs] = useState([]);

  const [sigMode, setSigMode] = useState("live");
  useEffect(() => { api.signalMode().then((d) => setSigMode(d?.mode ?? "live")).catch(() => {}); }, []);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({
    code: "600519.SH", direction: "buy", volume: 1000,
    algo: "twap", duration: 300, slices: 5, price_type: "market", limit_price: 0, remark: "",
  });

  async function load() {
    try { setJobs(await api.algoList()); } catch (e) { setErr(e.message); }
  }
  // P2-2：algo_slice/algo_alert 事件驱动近实时刷新，低频兜底轮询防丢失
  useActiveInterval(load, 30000);
  useServerEvents(["algo"], () => { load(); });

  const [pending, setPending] = useState(null);

  function requestSubmit() {
    setErr("");
    const total = Number(form.total_qty || 0);
    if (form.price_type === "limit" && !(Number(form.limit_price) > 0)) {
      setErr("限价模式必须填写限价（留空时后端会静默按最新价下单，与预期不符）"); return;
    }
    setPending({
      title: `确认启动 ${form.algo.toUpperCase()} 算法单`,
      rows: [
        { k: "标的", v: form.code }, { k: "方向", v: form.side === "buy" ? "买入" : "卖出" },
        { k: "总量", v: `${total} 股` }, { k: "时长/切片", v: `${form.duration}s / ${form.slices} 片` },
        { k: "价格", v: form.price_type === "limit" ? `限价 ${form.limit_price}` : "市价" },
      ],
      note: "将按切片真实提交委托（逐片过风控），可随时在列表中暂停/取消。",
      onConfirm: async () => {
        setPending(null);
        try { await api.algoSubmit(form); setErr(""); load(); }
        catch (e) { setErr(e.message); }
      },
    });
  }

  async function submit() {
    try { await api.algoSubmit(form); setErr(""); load(); }
    catch (e) { setErr(e.message); }
  }
  function actWithConfirm(id, fn, label) {
    setPending({
      title: `确认${label}`, rows: [{ k: "任务", v: id }],
      note: "该操作将作用于柜台真实委托（取消会撤未成交子单）。",
      onConfirm: async () => { setPending(null); await act(id, fn); },
    });
  }

  async function act(id, fn) {
    try { await fn(id); load(); } catch (e) { setErr(e.message); }
  }

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === "number" ? +e.target.value : e.target.value });
  const statusTag = (s) => {
    const m = orderStatus(s);
    return <span className={`tag ${m.className}`}>{m.label}</span>;
  };

  return (
    <div>
      <h2 className="page-title">算法交易 · TWAP/VWAP</h2>
      {sigMode !== "live" && (
        <span className="tag warn" style={{ marginLeft: 8 }}>
          信号模式：{sigMode === "paper" ? "模拟盘（不会真实下单）" : "预演（不会真实下单）"}—— 切换请到「信号路由」页
        </span>
      )}
      <p className="page-sub">按时间等分拆单执行，降低冲击成本；每片子单实时记录，支持暂停/恢复/取消（真实下单，过风控）</p>
      {err && <div className="toast err">{err}</div>}

      <div className="card">
        <h3>提交算法单</h3>
        <div className="row">
          <input placeholder="代码" value={form.code} onChange={set("code")} style={{ width: 130 }} />
          <select value={form.direction} onChange={set("direction")}>
            <option value="buy">买入</option><option value="sell">卖出</option>
          </select>
          <input type="number" placeholder="总量" value={form.volume} onChange={set("volume")} style={{ width: 110 }} />
          <select value={form.algo} onChange={set("algo")}>
            <option value="twap">TWAP</option><option value="vwap">VWAP</option>
          </select>
          <input type="number" placeholder="时长秒" value={form.duration} onChange={set("duration")} style={{ width: 110 }} />
          <input type="number" placeholder="切片数" value={form.slices} onChange={set("slices")} style={{ width: 100 }} />
        </div>
        <div className="row">
          <select value={form.price_type} onChange={set("price_type")}>
            <option value="market">市价</option><option value="limit">限价</option>
          </select>
          {form.price_type === "limit" && (
            <input type="number" step="0.01" placeholder="限价" value={form.limit_price}
                   onChange={set("limit_price")} style={{ width: 110 }} />
          )}
          <input placeholder="备注" value={form.remark} onChange={set("remark")} style={{ flex: 1 }} />
          <button onClick={requestSubmit}>提交算法单</button>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>算法单列表</h3>
        {jobs.length === 0 ? <p className="muted">暂无算法单</p> : (
          <table>
            <thead>
              <tr><th>ID</th><th>代码</th><th>方向</th><th>算法</th><th>总量</th>
                  <th>已成交</th><th>切片</th><th>状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              {jobs.slice().reverse().map((j) => (
                <tr key={j.algo_id}>
                  <td className="code">{j.algo_id}</td>
                  <td className="code">{j.code}</td>
                  <td>{j.direction === "buy" ? "买入" : "卖出"}</td>
                  <td>{j.algo.toUpperCase()}</td>
                  <td>{j.volume}</td>
                  <td>{j.done}</td>
                  <td>{j.slices_done}/{j.slices}</td>
                  <td>{statusTag(j.status)}</td>
                  <td>
                    {j.status === "running" && <button onClick={() => actWithConfirm(j.algo_id, api.algoPause, "暂停算法单")}>暂停</button>}
                    {j.status === "paused" && <button onClick={() => actWithConfirm(j.algo_id, api.algoResume, "恢复算法单")}>恢复</button>}
                    {["pending", "running", "paused"].includes(j.status) &&
                      <button onClick={() => actWithConfirm(j.algo_id, api.algoCancel, "取消算法单")}>取消</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {jobs.some((j) => j.error) && (
          <p className="muted" style={{ marginTop: 8 }}>最近错误：{jobs.find((j) => j.error)?.error}</p>
        )}
      </div>
    <ConfirmTradeModal pending={pending} busy={false} onClose={() => setPending(null)} risk="high" />
    </div>
  );
}
