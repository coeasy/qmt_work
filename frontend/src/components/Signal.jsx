import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 外部信号（Signal）
   信号路由：live/dry-run/paused 模式切换
   手动提交信号 / 二次确认
   入站 webhook 签名验证
*/

const MODES = [
  { key: "live",     label: "实时执行" },
  { key: "dry-run",  label: "模拟运行" },
  { key: "paused",   label: "暂停" },
];

export default function Signal() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState("live");

  /* 提交信号表单 */
  const [source, setSource] = useState("manual");
  const [code, setCode] = useState("");
  const [side, setSide] = useState("buy");
  const [volume, setVolume] = useState(100);
  const [price, setPrice] = useState("");
  const [priceType, setPriceType] = useState("limit");
  const [remark, setRemark] = useState("");
  const [pending, setPending] = useState(null);

  useEffect(() => { api.signalMode().then((d) => setMode(d?.mode ?? "live")).catch(() => {}); }, []);

  async function setSignalMode(m) {
    setLoading(true); setMsg(null);
    try {
      const r = await api.signalSetMode({ mode: m });
      setMode(r.mode);
      setMsg({ ok: true, t: `信号模式已切换为「${m}」` });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function submitSignal() {
    setLoading(true); setMsg(null);
    try {
      if (!code) throw new Error("请输入股票代码");
      if (!volume || volume < 100) throw new Error("数量至少 100 股");
      const body = {
        source, code, side, volume,
        price: price ? +price : undefined,
        price_type: priceType, remark,
      };
      const r = await api.signalSubmit(body);
      if (r && r.confirm_token) {
        setPending(r);
        setMsg({ ok: true, t: `信号已提交，等待二次确认（token: ${r.confirm_token.slice(0, 8)}...）` });
      } else {
        setMsg({ ok: true, t: `信号已执行：${code} ${side} ${volume}` });
      }
      setCode(""); setVolume(100); setPrice(""); setRemark("");
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function confirmSignal(totp) {
    setLoading(true); setMsg(null);
    try {
      if (!pending) throw new Error("无待确认信号");
      const r = await api.signalConfirm({ confirm_token: pending.confirm_token, totp_code: totp || "" });
      setMsg({ ok: true, t: "信号确认成功" });
      setPending(null);
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>外部信号 <span style={{ fontSize: 13, color: "#8aa0c4" }}>Signal Router</span></h2>
        <p>统一信号入口 · 支持手动 / webhook 入站 · HMAC 签名校验 · 大额二次确认</p>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      {/* 模式控制 */}
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>信号模式</h3>
        <div className="btn-group">
          {MODES.map((m) => (
            <button key={m.key} className={mode === m.key ? "active" : ""} onClick={() => setSignalMode(m.key)} disabled={loading}>
              {m.label}
            </button>
          ))}
          <span style={{ marginLeft: "auto", color: "#8aa0c4", display: "flex", alignItems: "center" }}>
            当前模式：<strong>{mode}</strong>
          </span>
        </div>
      </div>

      {/* 提交信号 */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>提交信号</h3>
        <div className="form-row">
          <div className="form-field">
            <label>来源</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="manual">手动</option>
              <option value="webhook">Webhook</option>
            </select>
          </div>
          <div className="form-field">
            <label>股票代码</label>
            <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="600519.SH" />
          </div>
          <div className="form-field">
            <label>方向</label>
            <div className="btn-group">
              <button className={side === "buy" ? "active" : ""} onClick={() => setSide("buy")}>买入</button>
              <button className={side === "sell" ? "active" : ""} onClick={() => setSide("sell")}>卖出</button>
            </div>
          </div>
          <div className="form-field">
            <label>数量</label>
            <input type="number" value={volume} onChange={(e) => setVolume(+e.target.value)} min={100} step={100} />
          </div>
          <div className="form-field">
            <label>委托类型</label>
            <div className="btn-group">
              <button className={priceType === "limit" ? "active" : ""} onClick={() => setPriceType("limit")}>限价</button>
              <button className={priceType === "market" ? "active" : ""} onClick={() => setPriceType("market")}>市价</button>
              <button className={priceType === "best" ? "active" : ""} onClick={() => setPriceType("best")}>最优</button>
            </div>
          </div>
          {priceType === "limit" && (
            <div className="form-field">
              <label>价格</label>
              <input type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} />
            </div>
          )}
        </div>
        <div className="form-row" style={{ marginTop: 8 }}>
          <div className="form-field" style={{ flex: 1 }}>
            <label>备注</label>
            <input value={remark} onChange={(e) => setRemark(e.target.value)} placeholder="交易原因或策略名称" />
          </div>
          <button className="btn-primary" onClick={submitSignal} disabled={loading || !code}>
            {loading ? "提交中…" : "提交信号"}
          </button>
        </div>

        {pending && (
          <div className="card" style={{ marginTop: 12, padding: 12 }}>
            <h4>⏳ 等待二次确认</h4>
            <p>信号：<strong>{pending.code} {pending.side} {pending.volume}</strong></p>
            <p>确认 token：<code>{pending.confirm_token}</code></p>
            <div className="form-row" style={{ marginTop: 8 }}>
              <input value={pending._totp || ""} onChange={(e) => setPending({ ...pending, _totp: e.target.value })}
                placeholder="TOTP 验证码（如已启用）" style={{ flex: 1 }} />
              <button className="btn-sm" onClick={() => confirmSignal(pending._totp)}>确认执行</button>
              <button className="btn-sm" onClick={() => setPending(null)}>取消</button>
            </div>
          </div>
        )}
      </div>

      {/* Webhook 入站说明 */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 8 }}>Webhook 入站</h3>
        <p style={{ fontSize: 12, color: "#9bb" }}>
          外部策略系统可通过 POST <code>/api/v1/signal/webhook</code> 提交信号。
          请求体示例：
        </p>
        <pre style={{ fontSize: 11, padding: 10, background: "rgba(0,0,0,0.2)", borderRadius: 4, overflowX: "auto" }}>
{JSON.stringify({
  "source": "webhook",
  "code": "600519.SH",
  "side": "buy",
  "volume": 100,
  "price": 220.0,
  "price_type": "limit",
  "remark": "外部策略触发"
}, null, 2)}
        </pre>
        <p style={{ fontSize: 12, color: "#9bb", marginTop: 8 }}>
          若后端配置了 <code>QMT_WEBHOOK_SECRET</code>，请求需携带 <code>X-Signature</code> 头（HMAC-SHA256 签名）。
        </p>
      </div>
    </div>
  );
}