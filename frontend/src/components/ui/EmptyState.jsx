// G11-5 统一空态组件（无数据/加载失败提示，避免裸表格）。
export default function EmptyState({ title = "暂无数据", hint = "", className = "" }) {
  return (
    <div className={`es-empty ${className}`.trim()}>
      <div className="es-icon">◻</div>
      <div className="es-title">{title}</div>
      {hint && <div className="es-hint">{hint}</div>}
    </div>
  );
}
