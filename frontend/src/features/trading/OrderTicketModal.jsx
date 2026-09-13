// 统一下单弹窗（快速下单单一封装）：预检 → 二次确认 → 真实下单，全部经 lib/tradeApi
// （字段归一化 + 幂等透传 + 错误翻译）。供涨停监控等列表页复用；MarketData 快速交易
// 抽屉保持内嵌表单形态，但提交链路同源。
// 用法：<OrderTicketModal open code name last limitPrice defaultSide onClose />
import { useEffect, useState } from "react";
import { sendOrder, precheckOrder } from "../../lib/tradeApi.js";
import ConfirmTradeModal from "../../components/ui/ConfirmTradeModal.jsx";
import { useBroker } from "../../BrokerContext.jsx";
import { usePlatform } from "../../PlatformContext.jsx";
import { api } from "../../api.js";
import { t } from "../../lib/i18n.js";

const DEFAULT_VOLUME = 100;

export default function OrderTicketModal({ open, code, name, last, limitPrice, defaultSide = "buy", onClose }) {
  const { activeId, brokers } = useBroker();
  const { can } = usePlatform();
  // C-P4：下单弹窗为真实交易动作，需 can("trading") 且已连接券商
  const tradingReady = can("trading") && !!activeId;
  const [side, setSide] = useState(defaultSide);
  const [price, setPrice] = useState("");
  const [volume, setVolume] = useState(DEFAULT_VOLUME);
  const [pre, setPre] = useState(null);       // {ok, allowed, reason}
  const [pending, setPending] = useState(null); // ConfirmTradeModal 待确认对象
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);       // {ok, t}
  const [connId, setConnId] = useState("");
  const [avail, setAvail] = useState(null);   // 卖出标的可用持仓（T+1）
  const [limits, setLimits] = useState(null); // {limit_up, limit_down}（涨跌停边界提示）

  // 打开/换标的/换方向：重置并预填默认价（买入优先涨停价抢封板，卖出优先最新价防挂失败）
  useEffect(() => {
    if (!open) return;
    setSide(defaultSide);
    const p = defaultSide === "buy"
      ? (limitPrice != null ? Number(limitPrice) : Number(last || 0))
      : Number(last || limitPrice || 0);
    setPrice(p > 0 ? p.toFixed(2) : "");
    setVolume(DEFAULT_VOLUME);
    setPre(null); setPending(null); setMsg(null);
    setAvail(null); setLimits(null);
    api.marketCapital({ codes: code }).then((d) => {
      const li = d?.limits?.[code];
      if (li) setLimits({ up: Number(li.limit_up), down: Number(li.limit_down) });
    }).catch(() => {});
    if (defaultSide === "sell" && code) {
      api.tradePositions().then((rows) => {
        const r = (Array.isArray(rows) ? rows : []).find(
          (x) => (x.code || x.symbol) === code);
        setAvail(r ? Number(r.available_volume ?? r.can_use_volume ?? r.volume) || 0 : 0);
      }).catch(() => setAvail(null));
    }
  }, [open, code, defaultSide]);

  if (!open) return null;

  const dirCn = side === "buy" ? "买入" : "卖出";
  const dirEn = side === "buy" ? "buy" : "sell";

  async function runPrecheck() {
    setMsg(null);
    setPre({ loading: true });
    const pc = await precheckOrder({ code, direction: dirEn, volume, price, price_type: "limit", conn_id: connId || undefined });
    setPre(pc);
  }

  async function doSubmit() {
    if (side === "sell" && avail != null && volume > avail) {
      setMsg({ ok: false, t: `可用持仓不足：可卖 ${avail} 股（T+1 规则下当日买入不可卖）` });
      setPending(null); return;
    }
    setBusy(true); setMsg(null);
    try {
      const d = await sendOrder({ code, direction: dirEn, volume, price, price_type: "limit", conn_id: connId || undefined });
      setMsg({ ok: true, t: `已报：${d?.order_id || "（等待回报）"}` });
      setPending(null);
    } catch (e) {
      setMsg({ ok: false, t: e.message || "提交失败" });
      setPending(null);
    } finally { setBusy(false); }
  }

  // 二次确认（真实资金操作）：预检通过后才允许进入
  function requestSubmit() {
    setMsg(null);
    setPending({
      title: `确认${dirCn}`,
      rows: [
        { k: "标的", v: `${code} ${name || ""}` },
        { k: "方向", v: dirCn },
        { k: "委托价", v: price },
        { k: "数量", v: `${volume} 股` },
      ],
      note: side === "buy" ? "将以限价单真实提交买入委托（过风控）。" : "将以限价单真实提交卖出委托（过风控）。",
      onConfirm: doSubmit,
    });
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal otm-card" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`快速下单 ${code}`}>
        <div className="ctm-title">快速下单 · {code} {name || ""}</div>
        <div className="qt-side" role="group" aria-label="买卖方向">
          <button className={`qt-side-btn buy ${side === "buy" ? "active" : ""}`}
                  onClick={() => setSide("buy")}>买入</button>
          <button className={`qt-side-btn sell ${side === "sell" ? "active" : ""}`}
                  onClick={() => setSide("sell")}>卖出</button>
        </div>
        {Array.isArray(brokers) && brokers.length > 1 && (
          <div className="row">
            <label style={{ width: 56 }}>账户</label>
            <select value={connId} onChange={(e) => setConnId(e.target.value)} style={{ flex: 1 }}>
              <option value="">活跃账户</option>
              {brokers.map((b) => <option key={b.id} value={b.id}>{b.name || b.id}</option>)}
            </select>
          </div>
        )}
        <div className="row">
          <label style={{ width: 56 }}>委托价</label>
          <input type="number" step="0.01" value={price}
                 onChange={(e) => setPrice(e.target.value)} aria-label="委托价" />
          {limitPrice != null && <span className="muted" style={{ fontSize: 11 }}>涨停 {Number(limitPrice).toFixed(2)}</span>}
        </div>
        <div className="row">
          <label style={{ width: 56 }}>数量</label>
          <input type="number" step="100" value={volume}
                 onChange={(e) => setVolume(Math.max(0, +e.target.value || 0))} aria-label="数量" />
        </div>
        <div className="btn-row">
          <button className="ghost btn-sm" onClick={runPrecheck} disabled={busy || !code}>
            {pre?.loading ? "预检中…" : "风控预检"}
          </button>
          <button className={`qt-submit ${side}`} disabled={busy || !code || !price || !tradingReady}
                  onClick={requestSubmit}>
            {dirCn}
          </button>
          <button className="ghost btn-sm" onClick={onClose}>关闭</button>
        </div>
        {!tradingReady && <div className="otm-pre err">交易未就绪：未连接券商或无交易权限，无法提交委托</div>}
        {limits && price !== "" && Number(price) > 0 && (
          Number(price) > limits.up || Number(price) < limits.down
        ) && (
          <div className="otm-pre err">委托价 {price} 超出涨跌停区间 [{limits.down}, {limits.up}]，柜台将拒单</div>
        )}
        {side === "buy" && limits && price !== "" && Number(price) >= limits.up && (
          <div className="otm-pre ok">涨停价委托（抢封板）：请注意封单量与开板风险</div>
        )}
        {side === "sell" && avail != null && (
          <div className={`otm-pre ${volume > avail ? "err" : "ok"}`}>
            可卖 {avail} 股{volume > avail ? "（不足，T+1 当日买入不可卖）" : ""}
          </div>
        )}
        {pre && !pre.loading && (
          <div className={`otm-pre ${pre.ok && pre.allowed !== false ? "ok" : "err"}`}>
            {pre.ok && pre.allowed !== false ? "✓ 风控预检通过" : `✗ ${pre.reason || "预检未通过"}`}
          </div>
        )}
        {msg && <div className={`toast ${msg.ok ? "ok" : "err"}`}>{msg.t}</div>}
        <ConfirmTradeModal pending={pending} busy={busy} onClose={() => setPending(null)} />
      </div>
    </div>
  );
}
