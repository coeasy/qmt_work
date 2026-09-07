import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import { useServerEvents } from "../hooks/useSystemWS.js";
import { consumePendingPrefill } from "../lib/trade.js";
import { sendOrder as _sendOrder, precheckOrder as _precheckOrder } from "../lib/tradeApi.js";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
import ConfirmTradeModal from "./ui/ConfirmTradeModal.jsx";

import usePersistentState from "../lib/usePersistentState.js";
import { t } from "../lib/i18n.js";
import { orderStatus } from "../lib/format.js";
import { on as onEvent, off as offEvent } from "../lib/eventBus";

// 手动交易面板：下单 / 持仓 / 委托 / 成交 / 条件单 / 目标仓位（全部真实接口，下单过风控）
// v3：支持叶子 params 直达预填（navTo("trade", {params}) 协议通道），
//     旧 trade:prefill 事件与 pending 消费兜底保留，双通道互不冲突。
export default function Trade({ params } = {}) {
  const { activeId, activeBroker } = useBroker();
  const [tab, setTab] = usePersistentState("trade:tab", "order");
  const [err, setErr] = useState("");
  const [pendingCond, setPendingCond] = useState(null);

  const [sigMode, setSigMode] = useState("live");
  useEffect(() => { api.signalMode().then((d) => setSigMode(d?.mode ?? "live")).catch(() => {}); }, []);
  const [msg, setMsg] = useState(null);
  const [precheck, setPrecheck] = useState(null);
  // T3：金融操作二次确认（提交订单/撤单/条件单/调仓计划）
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);

  const [form, setForm] = useState({
    code: "600519.SH", direction: "buy", volume: 100, price: 0, price_type: "limit",
  });
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [deals, setDeals] = useState([]);
  const [conds, setConds] = useState(null);
  const [condForm, setCondForm] = useState({
    code: "600519.SH", side: "buy", trigger_type: "lte",
    trigger_price: 1500, volume: 100, price_type: "market", price: 0,
  });
  const [targetForm, setTargetForm] = useState({ code: "600519.SH", target_pct: 0.1, price: 0, do_trade: false });

  async function wrap(fn, okText) {
    try { const r = await fn(); setMsg({ ok: true, t: okText || "操作成功" }); setErr(""); return r; }
    catch (e) { setMsg({ ok: false, t: e.message }); setErr(e.message); return null; }
  }

  async function load() {
    const [p, o, d, c] = await Promise.allSettled([
      api.tradePositions(), api.tradeOrders(), api.tradeDeals(), api.tradeConditions(),
    ]);
    if (p.status === "fulfilled") setPositions(p.value || []);
    if (o.status === "fulfilled") setOrders(o.value || []);
    if (d.status === "fulfilled") setDeals(d.value || []);
    if (c.status === "fulfilled") setConds(c.value);
    const fail = [p, o, d, c].find((x) => x.status === "rejected");
    if (fail && !err) setErr(fail.reason?.message || "");
  }
  // P2-2：WS 事件驱动近实时刷新（委托/成交/条件单/信号），并用 ≥30s 低频兜底轮询防事件丢失
  useActiveInterval(load, 30000, [activeId]);
  useServerEvents(["order", "trade", "condition", "signal", "algo", "reconcile"], () => { load(); });

  // 从涨停板 / 行情等页面「快速交易」带单过来：填充代码+涨停价并切到下单页
  useEffect(() => {
    const onPrefill = (detail) => {
      const d = detail || {};
      if (!d.code) return;
      setForm((f) => ({
        ...f, code: d.code,
        direction: d.direction || "buy",
        price: d.price ? Number(d.price) : f.price,
        price_type: d.price ? "limit" : f.price_type,
      }));
      setTab("order");
    };
    onEvent("trade:prefill", onPrefill);
    return () => offEvent("trade:prefill", onPrefill);
  }, []);

  // 若在挂载前就有人调用了 quickTradeNavigate（pending 已写入），立即消费，避免竞态丢单
  useEffect(() => {
    const p = consumePendingPrefill();
    if (p && p.code) {
      setForm((f) => ({
        ...f, code: p.code,
        direction: p.direction || "buy",
        price: p.price ? Number(p.price) : f.price,
        price_type: p.price ? "limit" : f.price_type,
      }));
      setTab("order");
    }
  }, []);

  // v3 通道：叶子 params 直达预填（打开/激活 trade tab 时生效一次）
  useEffect(() => {
    if (!params || !params.code) return;
    setForm((f) => ({
      ...f, code: params.code,
      direction: params.direction || "buy",
      price: params.price ? Number(params.price) : f.price,
      price_type: params.price ? "limit" : f.price_type,
    }));
    setTab("order");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params && params.code, params && params.price]);

  async function submitOrder() {
    // T3 二次确认：真实下单前弹窗核对（金额 = 价格×数量，市价单显示「市价」）
    setConfirm({
      title: "提交委托",
      rows: [
        { k: "方向", v: form.direction === "buy" ? "买入 BUY" : "卖出 SELL" },
        { k: "代码", v: form.code },
        { k: "价格", v: form.price_type === "limit" ? `¥${Number(form.price).toFixed(2)}` : "市价" },
        { k: "数量", v: `${form.volume} 股` },
        { k: "预估金额", v: form.price_type === "limit" ? `¥${(Number(form.price) * Number(form.volume)).toLocaleString()}` : "以成交价为准" },
      ],
      note: "委托将真实提交至券商，过风控校验后生效。",
      confirmText: "确认下单",
      onConfirm: async () => {
        setBusy(true);
        try {
          await wrap(() => _sendOrder(form).then((x) => { load(); return x; }), `已提交：${form.direction === "buy" ? "买入" : "卖出"} ${form.code} ${form.volume} 股`);
        } finally { setBusy(false); setConfirm(null); }
      },
    });
  }
  async function runPrecheck() {
    setPrecheck({ loading: true });
    try {
      // 统一走 tradeApi 预检（字段归一化 + 错误翻译，与快速交易/网格同链路）
      const pc = await _precheckOrder(form);
      setPrecheck({ ok: pc.ok && pc.allowed !== false, reason: pc.reason });
    } catch (e) { setPrecheck({ ok: false, reason: e.message }); }
  }
  async function cancelOrder(oid) {
    const o = orders.find((x) => x.order_id === oid);
    setConfirm({
      title: "撤单确认",
      rows: [
        { k: "委托号", v: oid },
        { k: "代码", v: o?.code || "—" },
        { k: "方向", v: o?.direction === "buy" ? "买入" : "卖出" },
        { k: "委托量", v: o?.volume != null ? `${o.volume} 股` : "—" },
      ],
      note: "撤单后未成交部分将被取消。",
      confirmText: "确认撤单",
      onConfirm: async () => {
        setBusy(true);
        try {
          await wrap(() => api.tradeCancel(oid), `已撤单：${oid}`); load();
        } finally { setBusy(false); setConfirm(null); }
      },
    });
  }
  async function submitCond() {
    setConfirm({
      title: "提交条件单",
      rows: [
        { k: "方向", v: condForm.side === "buy" ? "买入" : "卖出" },
        { k: "代码", v: condForm.code },
        { k: "触发", v: `${condForm.trigger_type === "gte" ? "≥" : "≤"} ¥${Number(condForm.trigger_price).toFixed(2)}` },
        { k: "数量", v: `${condForm.volume} 股` },
        { k: "下单方式", v: condForm.price_type === "market" ? "市价" : "限价" },
      ],
      note: "达到触发价后将自动真实下单（过风控）。",
      confirmText: "确认提交",
      onConfirm: async () => {
        setBusy(true);
        try {
          await wrap(() => api.tradeConditionSubmit(condForm), "条件单已提交（达到触发价自动下单）");
        } finally { setBusy(false); setConfirm(null); }
      },
    });
  }
  async function cancelCond(cid) {
    setPendingCond({ title: "确认取消条件单", rows: [{ k: "ID", v: cid }], note: "取消后不再触发。", onConfirm: async () => { setPendingCond(null); await wrap(() => api.tradeConditionCancel(cid), "条件单已取消"); } });
  }
  async function submitTarget() {
    // T3：调仓 = 真实下单，必须二次确认（无论是否勾选 do_trade，均展示将发生的动作）
    const doTrade = targetForm.do_trade;
    setConfirm({
      title: doTrade ? "生成调仓计划并下单" : "生成调仓计划（仅计划）",
      rows: [
        { k: "代码", v: targetForm.code },
        { k: "目标比例", v: `${(Number(targetForm.target_pct) * 100).toFixed(1)}%` },
        { k: "价格", v: targetForm.price ? `¥${Number(targetForm.price).toFixed(2)}` : "以现价成交" },
        { k: "执行方式", v: doTrade ? "实际下单（真实委托）" : "仅生成计划" },
      ],
      note: doTrade ? "勾选了「实际下单」：将按目标比例差额真实提交委托（过风控）。" : "未勾选「实际下单」，本次仅生成调仓计划。",
      confirmText: doTrade ? "确认下单" : "生成计划",
      onConfirm: async () => {
        setBusy(true);
        try {
          const r = await wrap(() => api.tradeTarget(targetForm), "目标仓位计划已生成");
          if (r && r.action === "trade") setMsg({ ok: true, t: `${r.direction === "buy" ? "买入" : "卖出"} ${r.code} ${r.volume} 股（目标 ${(r.target_pct * 100).toFixed(1)}%）` });
        } finally { setBusy(false); setConfirm(null); }
      },
    });
  }

  const set = (obj, fn) => (e) => fn({ ...obj, [e.target.name]: e.target.type === "number" ? +e.target.value : e.target.value });
  const setForm2 = set(form, setForm);
  const setCond = set(condForm, setCondForm);
  const setTarget = set(targetForm, setTargetForm);

  const noBroker = !activeBroker;
  const TABS = [["order", "下单"], ["positions", "持仓"], ["orders", "委托"], ["deals", "成交"], ["condition", "条件单"], ["target", "目标仓位"]];

  return (
    <div>
      <h2 className="page-title">{t(`page.trade.title`)}</h2>
      {sigMode !== "live" && (
        <span className="tag warn" style={{ marginLeft: 8 }}>
          信号模式：{sigMode === "paper" ? "模拟盘（不会真实下单）" : "预演（不会真实下单）"}—— 切换请到「信号路由」页
        </span>
      )}
      <p className="page-sub">
        真实下单（过风控），订单/成交实时状态推送
        {activeBroker && <span className="muted"> · {activeBroker.broker_name} · {activeBroker.account_id || "—"}</span>}
      </p>
      {msg && <div className={`toast ${msg.ok ? "ok" : "err"}`}>{msg.t}</div>}
      {err && !msg && <div className="toast err">{err}</div>}

      <div className="row" style={{ marginBottom: 12 }}>
        {TABS.map(([id, label]) => (
          <button key={id} onClick={() => setTab(id)}
                  style={tab === id ? { background: "var(--accent)", color: "#fff" } : {}}>{label}</button>
        ))}
      </div>

      {noBroker && tab !== "condition" && (
        <div className="empty-state">
          <p>尚未连接券商客户端。交易需真实券商连接（可在「券商连接」页添加）。</p>
        </div>
      )}

      {tab === "order" && (
        <div className="card">
          <h3>下单</h3>
          <div className="row">
            <input name="code" value={form.code} onChange={setForm2} placeholder="代码" style={{ width: 140 }} />
            <select name="direction" value={form.direction} onChange={setForm2}>
              <option value="buy">买入</option><option value="sell">卖出</option>
            </select>
            <input name="volume" type="number" value={form.volume} onChange={setForm2} style={{ width: 110 }} />
            <select name="price_type" value={form.price_type} onChange={setForm2}>
              <option value="limit">限价</option><option value="market">市价</option>
            </select>
            {form.price_type === "limit" && (
              <input name="price" type="number" step="0.01" value={form.price} onChange={setForm2} placeholder="限价" style={{ width: 110 }} />
            )}
            <button onClick={submitOrder}>提交订单</button>
            <button onClick={runPrecheck} disabled={precheck?.loading}>风控预检</button>
            {precheck && !precheck.loading && (
              <span className={`tag ${precheck.ok ? "ok" : "fail"}`}>
                {precheck.ok ? "预检通过" : "预检拦截"} · {precheck.reason}
              </span>
            )}
          </div>
          <p className="muted">风控：单笔金额/最小数量/单票持仓上限/频率限制将在后端强制校验。预检仅判断能否放行，不占用日级额度。</p>
        </div>
      )}

      {tab === "positions" && (
        <div className="card">
          <h3>当前持仓</h3>
          <table>
            <thead><tr><th>代码</th><th>名称</th><th>数量</th><th>可用</th><th>市值</th><th>成本</th></tr></thead>
            <tbody>
              {positions.map((p, i) => (
                <tr key={i}><td className="code">{p.code}</td><td>{p.name}</td><td>{p.volume}</td>
                  <td>{p.avail}</td><td>¥{(p.market_value ?? 0).toLocaleString()}</td><td>{p.cost}</td></tr>
              ))}
              {positions.length === 0 && <tr><td colSpan={6} className="muted">暂无持仓</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === "orders" && (
        <div className="card">
          <h3>当日委托</h3>
          <table>
            <thead><tr><th>委托号</th><th>代码</th><th>方向</th><th>价格</th><th>委托量</th><th>成交量</th><th>状态</th><th></th></tr></thead>
            <tbody>
              {orders.map((o, i) => (
                <tr key={i}><td className="code">{o.order_id}</td><td className="code">{o.code}</td>
                  <td>{o.direction === "buy" ? "买入" : "卖出"}</td><td>{o.price}</td>
                  <td>{o.volume}</td><td>{o.dealt}</td>
                  <td>{(() => { const st = orderStatus(o.status); return <span className={`tag ${st.className || "warn"}`}>{st.label}</span>; })()}</td>
                  <td><button onClick={() => cancelOrder(o.order_id)}>撤单</button></td></tr>
              ))}
              {orders.length === 0 && <tr><td colSpan={8} className="muted">暂无委托</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === "deals" && (
        <div className="card">
          <h3>当日成交</h3>
          <table>
            <thead><tr><th>委托号</th><th>代码</th><th>方向</th><th>价格</th><th>数量</th><th>时间</th></tr></thead>
            <tbody>
              {deals.map((d, i) => (
                <tr key={i}><td className="code">{d.order_id}</td><td className="code">{d.code}</td>
                  <td>{d.direction === "buy" ? "买入" : "卖出"}</td><td>{d.price}</td>
                  <td>{d.volume}</td><td>{d.time}</td></tr>
              ))}
              {deals.length === 0 && <tr><td colSpan={6} className="muted">暂无成交</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === "condition" && (
        <div className="grid grid-2">
          <div className="card">
            <h3>提交条件单 / 止损单</h3>
            <div className="row">
              <input name="code" value={condForm.code} onChange={setCond} style={{ width: 130 }} />
              <select name="side" value={condForm.side} onChange={setCond}>
                <option value="buy">买入</option><option value="sell">卖出</option>
              </select>
              <select name="trigger_type" value={condForm.trigger_type} onChange={setCond}>
                <option value="lte">价格 ≤ 触发价（低吸/止损）</option>
                <option value="gte">价格 ≥ 触发价（突破/止盈）</option>
              </select>
            </div>
            <div className="row">
              <input name="trigger_price" type="number" step="0.01" value={condForm.trigger_price} onChange={setCond} style={{ width: 130 }} />
              <input name="volume" type="number" value={condForm.volume} onChange={setCond} style={{ width: 110 }} />
              <select name="price_type" value={condForm.price_type} onChange={setCond}>
                <option value="market">市价触发后下单</option><option value="limit">限价触发后下单</option>
              </select>
            </div>
            <div className="btn-row"><button onClick={submitCond}>提交条件单</button></div>
          </div>
          <div className="card">
            <h3>条件单列表</h3>
            <table>
              <thead><tr><th>代码</th><th>方向</th><th>触发</th><th>量</th><th>状态</th><th></th></tr></thead>
              <tbody>
                {(conds?.orders || []).map((c, i) => (
                  <tr key={i}><td className="code">{c.code}</td><td>{c.side === "buy" ? "买入" : "卖出"}</td>
                    <td>{c.trigger_type === "gte" ? "≥" : "≤"}{c.trigger_price}</td><td>{c.volume}</td>
                    <td><span className={`tag ${c.status === "pending" ? "run" : c.status === "triggered" ? "ok" : "fail"}`}>{c.status}</span></td>
                    <td>{c.status === "pending" && <button onClick={() => cancelCond(c.id)}>取消</button>}</td></tr>
                ))}
                {!(conds?.orders || []).length && <tr><td colSpan={6} className="muted">暂无条件单</td></tr>}
              </tbody>
            </table>
            <p className="muted">触发后自动下单（过风控），状态实时更新</p>
          </div>
        </div>
      )}

      {tab === "target" && (
        <div className="card">
          <h3>目标仓位调仓</h3>
          <div className="row">
            <input name="code" value={targetForm.code} onChange={setTarget} style={{ width: 140 }} />
            <input name="target_pct" type="number" step="0.05" value={targetForm.target_pct} onChange={setTarget} style={{ width: 120 }} />
            <span className="muted">目标占总资产比例（0~1）</span>
            <input name="price" type="number" step="0.01" value={targetForm.price} onChange={setTarget} placeholder="价格(留空取现价)" style={{ width: 160 }} />
            <label><input name="do_trade" type="checkbox" checked={targetForm.do_trade} onChange={(e) => setTargetForm({ ...targetForm, do_trade: e.target.checked })} /> 实际下单</label>
          </div>
          <div className="btn-row"><button onClick={submitTarget}>生成调仓计划</button></div>
          <p className="muted">按当前总资产与持仓市值计算差额，折 100 股整；勾选「实际下单」才真正提交（过风控）</p>
        </div>
      )}

      <ConfirmTradeModal pending={confirm} busy={busy} onClose={() => !busy && setConfirm(null)} risk="high" />
    </div>
  );
}
