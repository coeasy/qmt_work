// 快速交易抽屉面板（自 MarketData.jsx 原样拆出，行为零变更）：
// 含预检 + 幂等 + 防双击 5s + 价格档自动跟随 tick。
import { useEffect, useRef, useState } from "react";
import { quickTradeNavigate } from "../../lib/trade.js";
import { sendOrder, precheckOrder } from "../../lib/tradeApi.js";
import { DEFAULT_VOLUME, PRICE_TICKS } from "./constants.js";
import ConfirmTradeModal from "../ui/ConfirmTradeModal.jsx";

export default function QuickTrade({ code, tick, stockInfo, last, pre, activeId, resetSignal = 0 }) {
  const [qtDir, setQtDir] = useState("buy");
  const [qtVol, setQtVol] = useState(DEFAULT_VOLUME);
  const [qtPriceKind, setQtPriceKind] = useState("ask1");
  const [qtPrice, setQtPrice] = useState("");
  const [qtSending, setQtSending] = useState(false);
  const [qtMsg, setQtMsg] = useState("");
  const [qtMsgType, setQtMsgType] = useState(""); // ok|err|info
  const lastSubmitRef = useRef(0); // 防双击 5s
  const [pending, setPending] = useState(null);

  // 订阅/换票：清空价格与消息（拆分前父组件的等价行为；价格档由 tick 自动跟随重填）
  useEffect(() => { setQtPrice(""); setQtMsg(""); setQtMsgType(""); }, [code, resetSignal]);

  // 快速交易价格档
  const priceOfKind = (kind) => {
    if (!last && !pre) return null;
    if (kind === "limit_up") return stockInfo?.high_limit != null ? Number(stockInfo.high_limit)
      : (tick?.high_limit != null ? Number(tick.high_limit) : null);
    if (kind === "limit_dn") return stockInfo?.low_limit != null ? Number(stockInfo.low_limit)
      : (tick?.low_limit != null ? Number(tick.low_limit) : null);
    if (kind === "ask1") return tick?.asks?.[0]?.price != null ? Number(tick.asks[0].price) : (last != null ? last : null);
    if (kind === "bid1") return tick?.bids?.[0]?.price != null ? Number(tick.bids[0].price) : (last != null ? last : null);
    if (kind === "last") return last != null ? last : null;
    return null;
  };
  // 自动跟随 tick 同步默认价格
  useEffect(() => {
    const p = priceOfKind(qtPriceKind);
    if (p != null) setQtPrice(String(p.toFixed(2)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qtPriceKind, tick?.last, tick?.asks?.[0]?.price, tick?.bids?.[0]?.price, stockInfo?.high_limit, stockInfo?.low_limit]);

  /* ---------- 快速交易提交（含预检 + 幂等 + 字段归一化） ---------- */
  async function doSubmit() {
    setPending(null); setQtSending(true);
    try {
      const d = await sendOrder({ code, direction: qtDir, volume: qtVol, price: qtPrice,
        price_type: "limit", conn_id: activeId });
      setQtMsg(`已报：${d?.order_id || "（等待回报）"}`);
      setQtMsgType("ok");
    } catch (e) {
      setQtMsg(e.message || "提交失败"); setQtMsgType("err");
    } finally { setQtSending(false); }
  }

  const submitOrder = async () => {
    setQtMsg(""); setQtMsgType("");
    if (!activeId) { setQtMsg("未连接券商：到「券商连接」添加后再下单"); setQtMsgType("err"); return; }
    if (!code) { setQtMsg("代码未填"); setQtMsgType("err"); return; }

    // 防双击 5s 窗口（与后端 single_flight 同窗口）
    const now = Date.now();
    if (now - lastSubmitRef.current < 5000) {
      setQtMsg("操作过快，请稍后再试"); setQtMsgType("err"); return;
    }

    setQtSending(true);
    try {
      // 1) 预检（在用户看得到的位置给出风控预警，TDX 同款"预演"）
      const pc = await precheckOrder({
        code, direction: qtDir, volume: qtVol, price: qtPrice,
        price_type: "limit", conn_id: activeId,
      });
      // 系统错误（无券商/网络）：直接显示并返回，不再尝试真下单
      if (!pc.ok) {
        setQtMsg("预检失败：" + pc.reason); setQtMsgType("err");
        lastSubmitRef.current = now; return;
      }
      // 风控拒绝：明确告知并退出
      if (pc.allowed === false) {
        setQtMsg("风控拦截：" + (pc.reason || "未通过风控预检")); setQtMsgType("err");
        lastSubmitRef.current = now; return;
      }
      // 2) 预检通过 → 二次确认（真实资金操作，与 OrderTicketModal 同标准）
      lastSubmitRef.current = now;
      setPending({
        title: `确认${qtDir === "buy" ? "买入" : "卖出"}`,
        rows: [
          { k: "标的", v: code },
          { k: "方向", v: qtDir === "buy" ? "买入" : "卖出" },
          { k: "委托价", v: qtPrice },
          { k: "数量", v: `${qtVol} 股` },
        ],
        note: "将以限价单真实提交委托（过风控）。",
        onConfirm: doSubmit,
      });
    } catch (e) {
      setQtMsg(e.message || "提交失败");
      setQtMsgType("err");
    } finally {
      setQtSending(false);
    }
  };

  return (
    <div className="card qt-card">
      <div className="qt-side">
        <button className={`qt-side-btn buy ${qtDir === "buy" ? "active" : ""}`}
          onClick={() => setQtDir("buy")}>买</button>
        <button className={`qt-side-btn sell ${qtDir === "sell" ? "active" : ""}`}
          onClick={() => setQtDir("sell")}>卖</button>
      </div>
      <div className="qt-row">
        <label>价格</label>
        <select value={qtPriceKind} onChange={(e) => setQtPriceKind(e.target.value)}>
          {PRICE_TICKS.map((t) => <option key={t.v} value={t.v}>{t.label}</option>)}
        </select>
      </div>
      <div className="qt-row">
        <label>委托价</label>
        <input type="number" step="0.01" value={qtPrice}
          onChange={(e) => setQtPrice(e.target.value)} />
      </div>
      <div className="qt-row">
        <label>数量</label>
        <input type="number" step="100" value={qtVol}
          onChange={(e) => setQtVol(Math.max(0, +e.target.value || 0))} />
      </div>
      <div className="qt-actions">
        <button className={`qt-submit ${qtDir}`} disabled={qtSending || !activeId}
          onClick={submitOrder}>
          {qtSending ? "提交中…" : (qtDir === "buy" ? "买入" : "卖出")}
        </button>
        <button className="ghost btn-sm"
          onClick={() => quickTradeNavigate(code, Number(qtPrice) || last, qtDir)}>
          委托面板
        </button>
      </div>
      {qtMsg && <div className={`qt-msg ${qtMsgType || "err"}`}>{qtMsg}</div>}
      <ConfirmTradeModal pending={pending} busy={qtSending} onClose={() => setPending(null)} risk="high" />
      {!activeId && <div className="muted qt-hint">未连接券商：可编辑但无法提交</div>}
    </div>
  );
}
