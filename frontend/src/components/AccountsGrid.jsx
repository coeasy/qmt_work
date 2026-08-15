// 多账户网格视图 + 批量操作：跨账户统一看板（资产/持仓矩阵 + 批量下单/撤单/重连）。
// 后端 /account/grid 与 /account/batch/* 是唯一真相来源；前端仅透传 conn_id，绝不内置券商逻辑。
import { useEffect, useRef, useState, Fragment } from "react";
import { api } from "../api.js";

const PRICE_TYPES = [
  { v: "limit", t: "限价" },
  { v: "market", t: "市价" },
];
const DIRECTIONS = [
  { v: "buy", t: "买入" },
  { v: "sell", t: "卖出" },
];

const fmt = (n) =>
  n == null ? "—" : Number(n).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString("zh-CN"));

function num(v, d = 0) {
  const x = d === 0 ? parseInt(v, 10) : parseFloat(v);
  return isNaN(x) ? 0 : x;
}

export default function AccountsGrid() {
  const [grid, setGrid] = useState(null);
  const [error, setError] = useState(null);     // 503 等友好提示
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [expanded, setExpanded] = useState({}); // 持仓矩阵展开状态（按 code）
  const timer = useRef(null);

  async function load() {
    setLoading(true);
    try {
      const g = await api.accountGrid();
      setGrid(g);
      setError(null);
      setUpdatedAt(new Date());
    } catch (e) {
      // 503：尚未添加/连接任何券商 —— 保留上一次数据，仅提示
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    return () => { if (timer.current) clearInterval(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (timer.current) clearInterval(timer.current);
    if (auto) timer.current = setInterval(load, 5000);
    return () => { if (timer.current) clearInterval(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto]);

  const conns = grid?.accounts || [];
  const connOptions = conns.map((c) => ({ conn_id: c.conn_id, name: c.name }));

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2 className="page-title">多账户网格</h2>
          <p className="page-sub">
            跨券商 / 跨账户统一看板：逐账户资产、持仓矩阵与批量下单 / 撤单 / 重连。未连接账户一并列出（error 说明原因）。
          </p>
        </div>
        <div className="row" style={{ gap: 10 }}>
          <label className="row" style={{ margin: 0, gap: 6 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            自动刷新（5s）
          </label>
          <button className="ghost" onClick={load} disabled={loading}>
            {loading ? "刷新中…" : "手动刷新"}
          </button>
          {updatedAt && <span className="muted" style={{ fontSize: 12 }}>更新于 {updatedAt.toLocaleTimeString("zh-CN")}</span>}
        </div>
      </div>

      {error && (
        <div className="empty-state">
          <div>{error}</div>
          <button onClick={load}>重试</button>
        </div>
      )}

      {/* 账户网格表 */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>账户看板（{conns.length}）</h3>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>名称</th><th>券商</th><th>账户</th><th>类型</th><th>状态</th>
                <th>总资产</th><th>可用金</th><th>市值</th><th>持仓</th><th>委托</th><th>成交</th><th>错误</th>
              </tr>
            </thead>
            <tbody>
              {conns.length === 0 && (
                <tr><td colSpan={12} className="muted">暂无账户数据（请先到「券商连接」添加并连接）</td></tr>
              )}
              {conns.map((c) => (
                <tr key={c.conn_id}>
                  <td>{c.name}</td>
                  <td>{c.broker}</td>
                  <td className="code">{c.account_id || "—"}</td>
                  <td className="muted">{c.account_type}</td>
                  <td>
                    <span className={`tag ${c.connected ? "ok" : "fail"}`}>
                      <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%",
                        background: c.connected ? "var(--accent-2)" : "var(--danger)", marginRight: 5 }} />
                      {c.connected ? "已连接" : "未连接"}
                    </span>
                  </td>
                  <td className="up">{fmt(c.assets)}</td>
                  <td>{fmt(c.cash)}</td>
                  <td>{fmt(c.market_value)}</td>
                  <td>{fmtInt(c.position_count)}</td>
                  <td>{fmtInt(c.order_count)}</td>
                  <td>{fmtInt(c.deal_count)}</td>
                  <td className="muted" style={{ color: c.error ? "var(--warn)" : undefined, maxWidth: 200 }}>{c.error || "—"}</td>
                </tr>
              ))}
            </tbody>
            {grid && (
              <tfoot>
                <tr style={{ fontWeight: 700 }}>
                  <td colSpan={4}>合计（{grid.account_count} 账户 · 已连接 {grid.connected_count}）</td>
                  <td></td>
                  <td className="up">{fmt(grid.total_assets)}</td>
                  <td>{fmt(grid.total_cash)}</td>
                  <td>{fmt(grid.total_market_value)}</td>
                  <td colSpan={4}></td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </div>

      {/* 跨账户持仓矩阵 */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>跨账户持仓矩阵（按标的汇总 · {grid?.positions?.length || 0}）</h3>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr><th>代码</th><th>名称</th><th>总持仓</th><th>总市值</th><th>持有账户</th></tr>
            </thead>
            <tbody>
              {(grid?.positions || []).length === 0 && (
                <tr><td colSpan={5} className="muted">暂无持仓</td></tr>
              )}
              {(grid?.positions || []).map((p) => {
                const open = !!expanded[p.code];
                return (
                  <Fragment key={p.code}>
                    <tr onClick={() => setExpanded((e) => ({ ...e, [p.code]: !e[p.code] }))}
                        style={{ cursor: "pointer" }}>
                      <td className="code">{p.code}</td>
                      <td>{p.name}</td>
                      <td>{fmtInt(p.total_volume)}</td>
                      <td className="up">{fmt(p.total_market_value)}</td>
                      <td className="muted">{p.accounts.length} 个账户 {open ? "▲" : "▼"}</td>
                    </tr>
                    {open && p.accounts.map((a, i) => (
                      <tr key={p.code + "_" + i} style={{ background: "rgba(0,0,0,.18)" }}>
                        <td colSpan={2} className="muted" style={{ paddingLeft: 28 }}>↳ {a.name}</td>
                        <td>{fmtInt(a.volume)}</td>
                        <td>{fmt(a.market_value)}</td>
                        <td className="muted">conn_id: {a.conn_id}</td>
                      </tr>
                    ))}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 批量操作面板 */}
      <div className="grid grid-3">
        <BatchOrder connOptions={connOptions} />
        <BatchCancel connOptions={connOptions} />
        <BatchReconnect connOptions={connOptions} />
      </div>
    </div>
  );
}

// ---------------- 批量下单 ----------------
function BatchOrder({ connOptions }) {
  const [mode, setMode] = useState("explicit"); // explicit | broadcast
  const [rows, setRows] = useState([blankOrder()]);
  const [broadcast, setBroadcast] = useState({ conn_ids: [], code: "", direction: "buy", volume: "", price: "", price_type: "limit" });
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  function setRow(i, patch) {
    setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  function addRow() { setRows((rs) => [...rs, blankOrder()]); }
  function delRow(i) { setRows((rs) => rs.filter((_, idx) => idx !== i)); }

  async function submit() {
    setBusy(true); setResult(null);
    try {
      const body = mode === "explicit"
        ? { orders: rows.filter((r) => r.code).map((r) => ({
            conn_id: r.conn_id, code: r.code.trim().toUpperCase(), direction: r.direction,
            volume: num(r.volume), price: num(r.price, 2), price_type: r.price_type,
          })) }
        : { conn_ids: broadcast.conn_ids, code: broadcast.code.trim().toUpperCase(),
            direction: broadcast.direction, volume: num(broadcast.volume),
            price: num(broadcast.price, 2), price_type: broadcast.price_type };
      setResult(await api.batchOrder(body));
    } catch (e) {
      setResult({ error: e.message });
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3>批量下单</h3>
      <label>模式</label>
      <select value={mode} onChange={(e) => setMode(e.target.value)}>
        <option value="explicit">逐单列表（多标的/多账户）</option>
        <option value="broadcast">广播同一指令（多账户）</option>
      </select>

      {mode === "explicit" ? (
        <div style={{ marginTop: 10 }}>
          {rows.map((r, i) => (
            <div key={i} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 8, marginBottom: 8 }}>
              <div className="grid grid-2" style={{ gap: 6 }}>
                <ConnSelect value={r.conn_id} options={connOptions} onChange={(v) => setRow(i, { conn_id: v })} />
                <input placeholder="代码" value={r.code} onChange={(e) => setRow(i, { code: e.target.value })} />
                <select value={r.direction} onChange={(e) => setRow(i, { direction: e.target.value })}>
                  {DIRECTIONS.map((d) => <option key={d.v} value={d.v}>{d.t}</option>)}
                </select>
                <input placeholder="数量" value={r.volume} onChange={(e) => setRow(i, { volume: e.target.value })} />
                <input placeholder="价格" value={r.price} onChange={(e) => setRow(i, { price: e.target.value })} />
                <select value={r.price_type} onChange={(e) => setRow(i, { price_type: e.target.value })}>
                  {PRICE_TYPES.map((p) => <option key={p.v} value={p.v}>{p.t}</option>)}
                </select>
              </div>
              <button className="ghost danger" style={{ marginTop: 6, padding: "4px 10px", fontSize: 12 }}
                onClick={() => delRow(i)} disabled={rows.length === 1}>删除此单</button>
            </div>
          ))}
          <button className="ghost" onClick={addRow}>+ 添加一笔</button>
        </div>
      ) : (
        <div className="grid grid-2" style={{ gap: 8, marginTop: 10 }}>
          <ConnMultiSelect value={broadcast.conn_ids} options={connOptions}
            onChange={(ids) => setBroadcast((b) => ({ ...b, conn_ids: ids }))} />
          <input placeholder="代码" value={broadcast.code} onChange={(e) => setBroadcast((b) => ({ ...b, code: e.target.value }))} />
          <select value={broadcast.direction} onChange={(e) => setBroadcast((b) => ({ ...b, direction: e.target.value }))}>
            {DIRECTIONS.map((d) => <option key={d.v} value={d.v}>{d.t}</option>)}
          </select>
          <input placeholder="数量" value={broadcast.volume} onChange={(e) => setBroadcast((b) => ({ ...b, volume: e.target.value }))} />
          <input placeholder="价格" value={broadcast.price} onChange={(e) => setBroadcast((b) => ({ ...b, price: e.target.value }))} />
          <select value={broadcast.price_type} onChange={(e) => setBroadcast((b) => ({ ...b, price_type: e.target.value }))}>
            {PRICE_TYPES.map((p) => <option key={p.v} value={p.v}>{p.t}</option>)}
          </select>
        </div>
      )}

      <div className="btn-row">
        <button onClick={submit} disabled={busy}>{busy ? "提交中…" : "提交批量下单"}</button>
      </div>
      {result && <ResultTable kind="order" result={result} />}
    </div>
  );
}

function blankOrder() {
  return { conn_id: "", code: "", direction: "buy", volume: "", price: "", price_type: "limit" };
}

// ---------------- 批量撤单 ----------------
function BatchCancel({ connOptions }) {
  const [mode, setMode] = useState("explicit");
  const [rows, setRows] = useState([{ conn_id: "", order_id: "" }]);
  const [broadcast, setBroadcast] = useState({ conn_ids: [], order_id: "" });
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  function setRow(i, patch) {
    setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  async function submit() {
    setBusy(true); setResult(null);
    try {
      const body = mode === "explicit"
        ? { items: rows.filter((r) => r.order_id).map((r) => ({ conn_id: r.conn_id, order_id: r.order_id })) }
        : { conn_ids: broadcast.conn_ids, order_id: broadcast.order_id };
      setResult(await api.batchCancel(body));
    } catch (e) {
      setResult({ error: e.message });
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3>批量撤单</h3>
      <label>模式</label>
      <select value={mode} onChange={(e) => setMode(e.target.value)}>
        <option value="explicit">逐单列表（conn_id + order_id）</option>
        <option value="broadcast">广播同一 order_id（多账户）</option>
      </select>

      {mode === "explicit" ? (
        <div style={{ marginTop: 10 }}>
          {rows.map((r, i) => (
            <div key={i} className="grid grid-2" style={{ gap: 6, border: "1px solid var(--border)", borderRadius: 8, padding: 8, marginBottom: 8 }}>
              <ConnSelect value={r.conn_id} options={connOptions} onChange={(v) => setRow(i, { conn_id: v })} />
              <input placeholder="委托号 order_id" value={r.order_id} onChange={(e) => setRow(i, { order_id: e.target.value })} />
              <button className="ghost danger" style={{ padding: "4px 10px", fontSize: 12 }}
                onClick={() => setRows((rs) => rs.filter((_, idx) => idx !== i))} disabled={rows.length === 1}>删除</button>
            </div>
          ))}
          <button className="ghost" onClick={() => setRows((rs) => [...rs, { conn_id: "", order_id: "" }])}>+ 添加一笔</button>
        </div>
      ) : (
        <div className="grid grid-2" style={{ gap: 8, marginTop: 10 }}>
          <ConnMultiSelect value={broadcast.conn_ids} options={connOptions}
            onChange={(ids) => setBroadcast((b) => ({ ...b, conn_ids: ids }))} />
          <input placeholder="委托号 order_id" value={broadcast.order_id}
            onChange={(e) => setBroadcast((b) => ({ ...b, order_id: e.target.value }))} />
        </div>
      )}

      <div className="btn-row">
        <button onClick={submit} disabled={busy}>{busy ? "提交中…" : "提交批量撤单"}</button>
      </div>
      {result && <ResultTable kind="cancel" result={result} />}
    </div>
  );
}

// ---------------- 批量重连 ----------------
function BatchReconnect({ connOptions }) {
  const [ids, setIds] = useState([]);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true); setResult(null);
    try {
      setResult(await api.batchReconnect({ conn_ids: ids }));
    } catch (e) {
      setResult({ error: e.message });
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3>批量重连</h3>
      <label>选择需重连的账户（留空 = 所有活跃连接）</label>
      <ConnMultiSelect value={ids} options={connOptions} onChange={setIds} />
      <div className="btn-row">
        <button onClick={submit} disabled={busy}>{busy ? "重连中…" : "提交批量重连"}</button>
      </div>
      {result && <ResultTable kind="reconnect" result={result} />}
    </div>
  );
}

// ---------------- 复用小组件 ----------------
function ConnSelect({ value, options, onChange }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— 选择连接 —</option>
      {options.map((o) => <option key={o.conn_id} value={o.conn_id}>{o.name}（{o.conn_id}）</option>)}
    </select>
  );
}

function ConnMultiSelect({ value, options, onChange }) {
  function toggle(id) {
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  }
  return (
    <div style={{ maxHeight: 160, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 8, padding: 6 }}>
      {options.length === 0 && <span className="muted" style={{ fontSize: 12 }}>无可用连接</span>}
      {options.map((o) => (
        <label key={o.conn_id} className="row" style={{ gap: 6, margin: "2px 0", fontSize: 12 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={value.includes(o.conn_id)} onChange={() => toggle(o.conn_id)} />
          {o.name}（{o.conn_id}）
        </label>
      ))}
    </div>
  );
}

function ResultTable({ kind, result }) {
  if (result.error) return <div className="toast err" style={{ position: "static", marginTop: 12 }}>{result.error}</div>;
  const res = result.results || [];
  return (
    <div style={{ marginTop: 12 }}>
      <div className="row" style={{ gap: 10, fontSize: 12, marginBottom: 6 }}>
        <span className="tag run">共 {result.total ?? res.length}</span>
        {result.ok != null && <span className="tag ok">成功 {result.ok}</span>}
      </div>
      <table>
        <thead>
          <tr>
            <th>连接</th>
            {kind === "order" && <><th>代码</th><th>方向</th><th>数量</th></>}
            {kind === "cancel" && <th>委托号</th>}
            {kind === "reconnect" && <th> </th>}
            <th>状态</th><th>明细</th>
            {kind === "order" && <th>委托ID</th>}
          </tr>
        </thead>
        <tbody>
          {res.map((r, i) => {
            const st = r.status;
            const okStatus = kind === "cancel" ? st === "canceled" : kind === "reconnect" ? st === "connected" : st === "submitted";
            return (
              <tr key={i}>
                <td className="code">{r.conn_id}</td>
                {kind === "order" && <><td className="code">{r.code}</td><td>{r.direction === "buy" ? "买" : "卖"}</td><td>{fmtInt(r.volume)}</td></>}
                {kind === "cancel" && <td className="code">{r.order_id}</td>}
                {kind === "reconnect" && <td></td>}
                <td><span className={`tag ${okStatus ? "ok" : "fail"}`}>{st}</span></td>
                <td className="muted" style={{ maxWidth: 220 }}>{r.detail}</td>
                {kind === "order" && <td className="code">{r.order_id ?? "—"}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
