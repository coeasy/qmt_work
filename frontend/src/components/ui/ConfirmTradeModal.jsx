import { useEffect, useRef, useState } from "react";

// 金融操作二次确认弹窗（T3/T19 统一）—— 替换 window.confirm 与裸一次点击下单。
//
// 用法：
//   const [pending, setPending] = useState(null);           // {title, rows:[{k,v}], onConfirm}
//   <ConfirmTradeModal pending={pending} busy={busy} onClose={() => setPending(null)} />
//
// 特性：ESC/背景点击关闭（危险操作要求「我已知晓」勾选才可确认，防误触）、
// 焦点陷阱（Tab 循环）、危险等级高亮（danger → 红主题）、busy 禁用。
export default function ConfirmTradeModal({ pending, busy, onClose, risk = "high" }) {
  const [ack, setAck] = useState(false);
  const overlayRef = useRef(null);
  const confirmRef = useRef(null);

  // 打开时重置勾选；ESC 关闭
  useEffect(() => {
    if (!pending) return undefined;
    setAck(false);
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [pending, onClose]);

  // 焦点陷阱：Tab 循环在弹窗内
  useEffect(() => {
    if (!pending) return undefined;
    const el = overlayRef.current;
    if (!el) return undefined;
    const focusables = () => el.querySelectorAll("button, input, [tabindex]:not([tabindex='-1'])");
    const onKeyDown = (e) => {
      if (e.key !== "Tab") return;
      const nodes = Array.from(focusables()).filter((n) => !n.disabled);
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    el.addEventListener("keydown", onKeyDown);
    return () => el.removeEventListener("keydown", onKeyDown);
  }, [pending]);

  if (!pending) return null;

  const canConfirm = risk !== "high" || ack;

  return (
    <div
      ref={overlayRef}
      className="ctm-overlay"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label={pending.title || "操作确认"}
    >
      <div className={`ctm-panel ${risk === "high" ? "ctm-danger" : ""}`}>
        <div className="ctm-head">
          <span className="ctm-title">{pending.title || "确认操作"}</span>
          <button className="ctm-close" onClick={() => !busy && onClose()} aria-label="关闭" disabled={busy}>×</button>
        </div>
        {risk === "high" && <div className="ctm-warn">此操作将真实提交委托/变更，请核对以下信息</div>}
        <div className="ctm-body">
          {(pending.rows || []).map((r, i) => (
            <div className="ctm-row" key={i}>
              <span className="ctm-k">{r.k}</span>
              <span className="ctm-v">{r.v}</span>
            </div>
          ))}
          {pending.note && <p className="ctm-note">{pending.note}</p>}
        </div>
        {risk === "high" && (
          <label className="ctm-ack">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} disabled={busy} />
            <span>我已知晓该操作会真实执行，确认继续</span>
          </label>
        )}
        <div className="ctm-actions">
          <button className="btn btn-sm" onClick={onClose} disabled={busy}>取消</button>
          <button
            ref={confirmRef}
            className="btn btn-sm btn-danger-sm"
            disabled={busy || !canConfirm}
            onClick={() => { if (pending.onConfirm) pending.onConfirm(); }}
          >
            {busy ? "执行中…" : (pending.confirmText || "确认执行")}
          </button>
        </div>
      </div>
    </div>
  );
}
