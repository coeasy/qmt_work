import { useEffect, useState } from "react";
import { api, getApiKey, setApiKey } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";

export default function Settings() {
  const { brokers, connectedCount, activeBroker } = useBroker();
  const [cfg, setCfg] = useState({ provider: "openai", base_url: "", api_key: "", model: "", temperature: 0.2 });
  const [masked, setMasked] = useState("");
  const [keys, setKeys] = useState([]);
  const [newKey, setNewKey] = useState("");
  const [msg, setMsg] = useState(null);
  const [apiKeyLocal, setApiKeyLocal] = useState(getApiKey());
  const isElectron = typeof window !== "undefined" && !!window.electronAPI;
  const [autoLaunch, setAutoLaunch] = useState(null);
  const [riskCfg, setRiskCfg] = useState(null);
  const [runtimeCfg, setRuntimeCfg] = useState(null);
  const [hist, setHist] = useState([]);
  const [riskDaily, setRiskDaily] = useState(null);

  useEffect(() => {
    api.getRiskConfig().then(setRiskCfg).catch(() => {});
    // 守护：API 返回非对象时强制转为 {}，避免下游 Object.entries().map 抛 "_.map is not a function"
    api.getRuntimeConfig().then((r) => setRuntimeCfg(r && typeof r === "object" && !Array.isArray(r) ? r : {})).catch(() => {});
    api.riskDaily().then(setRiskDaily).catch(() => {});
    // 守护：history 必须是数组；后端返回 {items:[...]} 等包装结构时解包
    api.runtimeHistory().then((r) => setHist(Array.isArray(r) ? r : (Array.isArray(r?.items) ? r.items : []))).catch(() => {});
  }, []);

  async function saveRuntime() {
    try {
      // 只提交非空的运行时配置（含 desc/默认值展示字段需剔除）
      const body = {};
      Object.entries(runtimeCfg).forEach(([k, v]) => {
        if (v && typeof v.value === "number") body[k] = v.value;
      });
      await api.putRuntimeConfig(body);
      setMsg({ ok: true, t: "运行参数已保存并立即生效（无需重启）" });
      api.getRuntimeConfig().then(setRuntimeCfg).catch(() => {});
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }
  function saveApiKeyLocal() {
    setApiKey(apiKeyLocal);
    setMsg({ ok: true, t: apiKeyLocal.trim() ? "API Key 已保存，后续请求自动附带" : "已清除 API Key（本机回环访问无需密钥）" });
  }
  const setRuntime = (k) => (e) => setRuntimeCfg({
    ...runtimeCfg, [k]: { ...runtimeCfg[k], value: parseFloat(e.target.value) },
  });

  async function circuitTrip() {
    try {
      await api.riskCircuit({ action: "trip", reason: "手动熔断：暂停买入开仓" });
      setMsg({ ok: true, t: "已触发熔断：买入开仓被禁止（卖出平仓不受影响）" });
      api.riskDaily().then(setRiskDaily).catch(() => {});
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }
  async function circuitReset() {
    try {
      await api.riskCircuit({ action: "reset" });
      setMsg({ ok: true, t: "已解除熔断，日初净值重新锚定" });
      api.riskDaily().then(setRiskDaily).catch(() => {});
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }

  async function saveRisk() {
    try {
      await api.putRiskConfig(riskCfg);
      setMsg({ ok: true, t: "风控参数已保存并生效" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }
  const setRisk = (k) => (e) => setRiskCfg({ ...riskCfg, [k]: +e.target.value });
  const setRiskStr = (k) => (e) => setRiskCfg({ ...riskCfg, [k]: e.target.value });

  useEffect(() => {
    if (isElectron) {
      window.electronAPI.getAutoLaunch().then((r) => setAutoLaunch(!!r.enabled)).catch(() => {});
    }
  }, []);

  async function toggleAutoLaunch() {
    try {
      const r = await window.electronAPI.setAutoLaunch(!autoLaunch);
      setAutoLaunch(!!r.enabled);
      setMsg({ ok: r.ok, t: r.ok ? (r.enabled ? "已开启开机自启" : "已关闭开机自启") : `操作失败：${r.error || ""}` });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }

  async function load() {
    try {
      const c = await api.get("/config/llm");
      setCfg((p) => ({ ...p, provider: c.provider, base_url: c.base_url, model: c.model, temperature: c.temperature }));
      setMasked(c.configured ? c.api_key_masked : "（未配置）");
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    try {
      // 守护：/api-keys 可能是分页结构 {items:[...]} 或其他；统一为数组
      const r = await api.get("/api-keys");
      setKeys(Array.isArray(r) ? r : (Array.isArray(r?.items) ? r.items : (Array.isArray(r?.keys) ? r.keys : [])));
    } catch {}
  }
  useEffect(() => { load(); }, []);

  async function saveLLM() {
    setMsg(null);
    try {
      // 仅当用户填写了 api_key 才覆盖；留空则保留已存密钥
      await api.put("/config/llm", { ...cfg, api_key: cfg.api_key || "" });
      setMsg({ ok: true, t: "模型供应商已保存" });
      setCfg((p) => ({ ...p, api_key: "" }));
      load();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }

  async function createKey() {
    try {
      const r = await api.createApiKey({ name: newKey || "default" });
      setMsg({ ok: true, t: `已生成 API Key：${r.api_key}（仅显示一次，请妥善保存）` });
      setNewKey(""); load();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }
  async function rotateKey(kid) {
    try {
      const r = await api.rotateApiKey(kid);
      setMsg({ ok: true, t: `已轮换 #${kid}：新密钥 ${r.api_key}（仅显示一次，旧密钥已失效）` });
      load();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }
  async function deleteKey(kid) {
    if (!window.confirm(`确认删除 API Key #${kid}？删除后该密钥立即失效，且无法恢复。`)) return;
    try { await api.deleteApiKey(kid); setMsg({ ok: true, t: `已删除 #${kid}` }); load(); }
    catch (e) { setMsg({ ok: false, t: e.message }); }
  }
  async function toggleStatus(k) {
    const next = k.status === "active" ? "disabled" : "active";
    try {
      await api.patchApiKey(k.id, { status: next });
      setMsg({ ok: true, t: `#${k.id} 已${next === "active" ? "启用" : "停用"}` }); load();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }

  async function loadHist() {
    try { setHist(await api.runtimeHistory()); } catch {}
  }
  async function rollback(id) {
    try {
      await api.runtimeRollback(id);
      setMsg({ ok: true, t: `已回滚到配置历史 #${id}` });
      api.getRuntimeConfig().then(setRuntimeCfg).catch(() => {}); loadHist();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  }

  return (
    <div>
      <h2 className="page-title">设置</h2>
      <p className="page-sub">模型供应商（任意 OpenAI 兼容 / Anthropic 接口，可留空用默认）与第三方 API Key</p>
      {msg && <div className={`toast ${msg.ok ? "ok" : "err"}`}>{msg.t}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>券商连接状态</h3>
        <div className="row">
          <span className={`tag ${connectedCount ? "ok" : "fail"}`}>{connectedCount} 个在线</span>
          {activeBroker && <span className="tag run">活跃：{activeBroker.broker_name} · {activeBroker.account_id || "—"}</span>}
          <span className="muted">共 {brokers.length} 个已配置连接</span>
          <button className="ghost" onClick={() => window.dispatchEvent(new CustomEvent("nav", { detail: "brokers" }))}>
            前往「券商连接」管理
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>远程访问 API Key（可选）</h3>
        <p className="muted" style={{ marginTop: 4 }}>
          本机浏览器访问无需填写（回环免鉴权）。通过局域网 / 远程服务器访问本平台 UI 或调用 API 时，
          填写服务端配置的主密钥（<code>api_key</code>）或已签发的子密钥，保存后自动附带于所有请求头。
          Key 仅保存在当前浏览器 localStorage，不会上传。
        </p>
        <div className="row" style={{ marginTop: 8 }}>
          <input
            type="password"
            style={{ minWidth: 320 }}
            placeholder="粘贴 API Key（qmt-xxx 或主密钥）"
            defaultValue={apiKeyLocal}
            onChange={(e) => setApiKeyLocal(e.target.value)}
          />
          <button onClick={saveApiKeyLocal}>保存并应用到所有请求</button>
        </div>
      </div>

      {isElectron && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h3>客户端（全自动运行）</h3>
          <div className="row">
            <span>开机自启（随 Windows 启动自动拉起平台）</span>
            <button onClick={toggleAutoLaunch}>
              {autoLaunch ? "已开启，点击关闭" : "已关闭，点击开启"}
            </button>
            <span className={`tag ${autoLaunch ? "ok" : "warn"}`}>{autoLaunch ? "开机自启" : "未开启"}</span>
          </div>
        </div>
      )}

      {riskCfg && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h3>风控参数（下单实时生效）</h3>
          {riskCfg.data_source !== "live" && (
            <div className="msg error" style={{ marginBottom: 10 }}>
              当前风控持仓/总资产为<strong>演示值（默认 ¥100 万）</strong>：未接入真实账户快照。
              连接券商并积累账户快照后自动切换为真实持仓。
            </div>
          )}
          <div className="grid grid-3">
            <div><label>单笔金额上限 ¥</label>
              <input type="number" value={riskCfg.max_amount} onChange={setRisk("max_amount")} /></div>
            <div><label>最小数量（股）</label>
              <input type="number" value={riskCfg.min_qty} onChange={setRisk("min_qty")} /></div>
            <div><label>全局持仓占比上限</label>
              <input type="number" step="0.05" value={riskCfg.max_position_ratio} onChange={setRisk("max_position_ratio")} /></div>
            <div><label>单票持仓占比上限</label>
              <input type="number" step="0.05" value={riskCfg.max_single_position_ratio} onChange={setRisk("max_single_position_ratio")} /></div>
            <div><label>每分钟最大下单数</label>
              <input type="number" value={riskCfg.max_orders_per_min} onChange={setRisk("max_orders_per_min")} /></div>
            <div><label>日累计下单金额上限 ¥（0=关闭）</label>
              <input type="number" value={riskCfg.daily_amount_limit ?? 0} onChange={setRisk("daily_amount_limit")} /></div>
            <div><label>日亏损熔断阈值 ¥（0=关闭）</label>
              <input type="number" value={riskCfg.daily_loss_limit ?? 0} onChange={setRisk("daily_loss_limit")} /></div>
            <div><label>单标的日下单次数上限（0=关闭）</label>
              <input type="number" value={riskCfg.per_code_daily_orders ?? 0} onChange={setRisk("per_code_daily_orders")} /></div>
            <div><label>价格偏离拒单 ±%（0=关闭）</label>
              <input type="number" step="0.01" min="0" value={riskCfg.price_deviation_pct ?? 0}
                     onChange={setRisk("price_deviation_pct")} /></div>
            <div><label>标的白名单（逗号分隔，空=全部）</label>
              <input value={riskCfg.symbol_allow ?? ""} placeholder="600519.SH,000001.SZ"
                     onChange={setRiskStr("symbol_allow")} /></div>
            <div><label>标的黑名单（逗号分隔，命中即拒）</label>
              <input value={riskCfg.symbol_deny ?? ""} placeholder="600000.SH"
                     onChange={setRiskStr("symbol_deny")} /></div>
          </div>
          <div className="btn-row" style={{ marginTop: 10 }}>
            <button onClick={saveRisk}>保存风控参数</button>
            {riskDaily && (
              <span className={`tag ${riskDaily.circuit_broken ? "fail" : "ok"}`}
                    style={{ marginLeft: 8 }}>
                熔断：{riskDaily.circuit_broken ? "已触发" : "正常"}
                {riskDaily.circuit_broken && ` · ${riskDaily.circuit_reason}`}
              </span>
            )}
            {riskDaily && !riskDaily.circuit_broken && (
              <button className="danger" onClick={circuitTrip}>一键熔断（停买）</button>
            )}
            {riskDaily && riskDaily.circuit_broken && (
              <button className="ghost" onClick={circuitReset}>解除熔断</button>
            )}
          </div>
          {riskDaily && (
            <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
              今日：已下单 ¥{riskDaily.day_amount ?? 0}
              {riskDaily.day_amount_limit > 0 && ` / 上限 ¥${riskDaily.day_amount_limit}（${riskDaily.day_amount_used_pct ?? 0}%）`}
              {" · "}当前净值 ¥{riskDaily.current_net ?? "—"}
              {riskDaily.drawdown > 0 && ` · 日内回撤 ¥${riskDaily.drawdown}`}
            </div>
          )}
        </div>
      )}

      {runtimeCfg && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h3>引擎运行参数（热更新，无需重启）</h3>
          <div className="grid grid-3">
            {Object.entries(runtimeCfg).map(([k, v]) => (
              <div key={k} title={`${v.desc}（默认 ${v.default}）`}>
                <label>{k} {v.overridden && <span className="tag ok">已覆盖</span>}</label>
                <input type="number" step="0.1" min={v.min} value={v.value}
                       onChange={setRuntime(k)} />
                <div className="muted" style={{ fontSize: 11 }}>{v.desc}</div>
              </div>
            ))}
          </div>
          <div className="btn-row" style={{ marginTop: 10 }}><button onClick={saveRuntime}>保存并热生效</button></div>
          <details style={{ marginTop: 12 }}>
            <summary style={{ cursor: "pointer" }} className="muted">变更历史与回滚（共 {hist.length} 条）</summary>
            <table style={{ marginTop: 8 }}>
              <thead><tr><th>ID</th><th>配置项</th><th>动作</th><th>旧值</th><th>新值</th><th>时间</th><th>操作</th></tr></thead>
              <tbody>
                {hist.map((h) => (
                  <tr key={h.id}>
                    <td className="code">{h.id}</td>
                    <td>{h.key}</td>
                    <td><span className="tag run">{h.action}</span></td>
                    <td className="muted" style={{ fontSize: 11, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>{h.old_value || "—"}</td>
                    <td className="muted" style={{ fontSize: 11, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>{h.new_value || "—"}</td>
                    <td className="code" style={{ fontSize: 11 }}>{h.created_at}</td>
                    <td><button className="ghost" onClick={() => rollback(h.id)}>回滚</button></td>
                  </tr>
                ))}
                {hist.length === 0 && <tr><td colSpan={7} className="muted">暂无变更历史</td></tr>}
              </tbody>
            </table>
          </details>
        </div>
      )}

      <div className="grid grid-2">
        <div className="card">
          <h3>模型供应商</h3>
          <label>供应商类型</label>
          <select value={cfg.provider} onChange={(e) => setCfg({ ...cfg, provider: e.target.value })}>
            <option value="openai">OpenAI 兼容</option>
            <option value="anthropic">Anthropic</option>
          </select>
          <label>Base URL</label>
          <input value={cfg.base_url} placeholder="https://api.deepseek.com/v1" onChange={(e) => setCfg({ ...cfg, base_url: e.target.value })} />
          <label>API Key {masked && <span className="muted">（当前：{masked}）</span>}</label>
          <input type="password" value={cfg.api_key} placeholder="留空则不修改已存密钥" onChange={(e) => setCfg({ ...cfg, api_key: e.target.value })} />
          <label>模型</label>
          <input value={cfg.model} placeholder="deepseek-chat" onChange={(e) => setCfg({ ...cfg, model: e.target.value })} />
          <label>温度</label>
          <input type="number" step="0.1" min="0" max="2" value={cfg.temperature} onChange={(e) => setCfg({ ...cfg, temperature: parseFloat(e.target.value) })} />
          <div className="btn-row"><button onClick={saveLLM}>保存</button></div>
        </div>

        <div className="card">
          <h3>第三方 API Key（列表 / 创建 / 轮换 / 停用 / 删除）</h3>
          <div className="row">
            <input style={{ flex: 1 }} value={newKey} placeholder="名称" onChange={(e) => setNewKey(e.target.value)} />
            <button onClick={createKey}>生成</button>
            <span className="muted">创建 / 轮换后仅显示一次完整密钥，请妥善保存</span>
          </div>
          <table style={{ marginTop: 12 }}>
            <thead><tr><th>前缀</th><th>名称</th><th>范围</th><th>状态</th><th>过期</th><th>操作</th></tr></thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td className="code">{k.key_prefix}</td>
                  <td>{k.name}</td>
                  <td className="muted" style={{ fontSize: 11 }}>{k.scopes || "—"}</td>
                  <td><span className={`tag ${k.status === "active" ? "ok" : "fail"}`}>{k.status}</span></td>
                  <td className="muted" style={{ fontSize: 11 }}>{k.expires_at || "—"}</td>
                  <td className="row" style={{ gap: 6 }}>
                    <button className="ghost" onClick={() => rotateKey(k.id)}>轮换</button>
                    <button className="ghost" onClick={() => toggleStatus(k)}>
                      {k.status === "active" ? "停用" : "启用"}
                    </button>
                    <button className="danger" onClick={() => deleteKey(k.id)}>删除</button>
                  </td>
                </tr>
              ))}
              {keys.length === 0 && <tr><td colSpan={6} className="muted">暂无 Key</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
