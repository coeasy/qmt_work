import { useState } from "react";

// 通用批量删除操作条：当有选中行时浮出，显示数量，二次确认后回调 onDelete(ids)。
// count：已选数量；onDelete：批量删除回调（接收选中 id 数组）；onClear：清空选择。
// busy：删除进行中禁用按钮。
export default function BatchDeleteBar({ count, onDelete, onClear, busy, label = "数据" }) {
  const [confirm, setConfirm] = useState(false);

  if (!count || count <= 0) return null;

  const commit = () => {
    if (busy) return;
    if (!confirm) { setConfirm(true); return; }
    onDelete();
    setConfirm(false);
  };
  const cancel = () => { setConfirm(false); onClear(); };

  return (
    <div className="batch-bar">
      <span className="batch-count">已选 {count} 项{label ? ` ${label}` : ""}</span>
      {confirm ? (
        <span className="batch-confirmtxt">确认删除这 {count} 项？</span>
      ) : null}
      <button className="btn btn-sm btn-danger-sm" onClick={commit} disabled={busy}>
        {busy ? "删除中…" : confirm ? "确认删除" : "批量删除"}
      </button>
      <button className="btn btn-sm" onClick={cancel} disabled={busy}>清空</button>
    </div>
  );
}