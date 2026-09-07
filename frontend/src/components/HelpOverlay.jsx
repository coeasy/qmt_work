// 帮助层（F1）：列出快捷键与功能分组，便于通达信式键盘流上手。
import { useEffect, useState } from "react";
import { PAGE_TREE } from "../pagesRegistry.jsx";
import { on as onEvent, off as offEvent } from "../lib/eventBus";

const SHORTCUTS = [
  { k: "Ctrl/Cmd + K", d: "打开命令面板（模糊跳页 / 代码直达个股）" },
  { k: "Alt + 1…9", d: "跳转到第 N 个功能分组的首个页面" },
  { k: "F1", d: "打开/关闭本帮助" },
  { k: "F3", d: "上证指数（000001.SH 个股分析）" },
  { k: "F4", d: "深证成指（399001.SZ 个股分析）" },
  { k: "F5", d: "分时 ↔ K线 切换（个股分析页）" },
  { k: "F6", d: "报价牌 → 自选股" },
  { k: "F10", d: "聚焦 F10 资料块（个股分析页）" },
  { k: "F12", d: "跳到交易页" },
  { k: "Esc", d: "关闭命令面板 / 帮助" },
  { k: "Enter / ↑↓", d: "命令面板内选择并打开" },
];

export default function HelpOverlay() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onToggle = () => setOpen((o) => !o);
    const onClose = () => setOpen(false);
    onEvent("help:toggle", onToggle);
    onEvent("help:close", onClose);
    return () => {
      offEvent("help:toggle", onToggle);
      offEvent("help:close", onClose);
    };
  }, []);

  if (!open) return null;
  return (
    <div className="cmd-overlay" onClick={() => setOpen(false)}>
      <div className="help-panel" onClick={(e) => e.stopPropagation()}>
        <div className="help-title">快捷键与功能导航</div>
        <div className="help-section">
          <div className="help-h">快捷键</div>
          <table className="help-table">
            <tbody>
              {SHORTCUTS.map((s) => (
                <tr key={s.k}><td className="help-k">{s.k}</td><td>{s.d}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="help-section">
          <div className="help-h">功能分组（Alt+数字直达）</div>
          <div className="help-groups">
            {PAGE_TREE.map((g, i) => (
              <div className="help-group" key={g.group}>
                <span className="help-num">Alt+{i + 1}</span>
                <span className="help-gname">{g.group}</span>
                <span className="help-items">{g.items.map((it) => it.label).join("、")}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="cmd-hint">点击空白处或按 Esc 关闭</div>
      </div>
    </div>
  );
}
