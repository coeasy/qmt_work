// 命令面板（Ctrl/Cmd+K）：模糊检索页面并打开，类似 VS Code 命令面板。
import { useEffect, useMemo, useRef, useState } from "react";
import { PAGE_TREE } from "../pagesRegistry.jsx";
import { navTo, navToQuote } from "../lib/nav.js";

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const inputRef = useRef(null);

  // 扁平化可检索条目
  const items = useMemo(() => {
    const out = [];
    PAGE_TREE.forEach((g) => g.items.forEach((it) => out.push({ key: it.key, label: it.label, group: g.group })));
    return out;
  }, []);

  // 股票代码直达：6 位数字（可选 .SH/.SZ）-> 个股分析页
  const CODE_RE = /^\d{6}(\.(sh|sz))?$/i;
  const normCode = (raw) => {
    let c = raw.trim().toUpperCase();
    if (/\.(SH|SZ)$/.test(c)) return c;
    const num = c.slice(0, 6);
    let ex = "SH";
    if (/^(00|30)/.test(num)) ex = "SZ";
    else if (/^(60|68|9)/.test(num)) ex = "SH";
    return num + "." + ex;
  };
  const stockAction = CODE_RE.test(q.trim())
    ? { key: "__stock__", label: `打开个股分析：${normCode(q)}`, group: "个股" }
    : null;

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return items;
    return items.filter((it) =>
      it.label.toLowerCase().includes(s) || it.key.toLowerCase().includes(s) || it.group.toLowerCase().includes(s));
  }, [q, items]);

  // 最终列表：股票代码直达置顶
  const list = useMemo(() => (stockAction ? [stockAction, ...filtered] : filtered), [stockAction, filtered]);

  useEffect(() => {
    const onToggle = () => { setOpen((o) => !o); setQ(""); setIdx(0); };
    const onClose = () => setOpen(false);
    window.addEventListener("cmd:toggle", onToggle);
    window.addEventListener("cmd:close", onClose);
    return () => {
      window.removeEventListener("cmd:toggle", onToggle);
      window.removeEventListener("cmd:close", onClose);
    };
  }, []);

  useEffect(() => { if (open && inputRef.current) inputRef.current.focus(); }, [open]);
  useEffect(() => { setIdx(0); }, [q]);

  if (!open) return null;

  const choose = (it) => {
    if (!it) return;
    if (it.key === "__stock__") {
      // v3：nav 协议带参直达，不再写 sessionStorage
      navToQuote(normCode(q));
    } else {
      navTo(it.key);
    }
    setOpen(false);
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(i + 1, list.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(list[idx]); }
  };

  return (
    <div className="cmd-overlay" onClick={() => setOpen(false)}>
      <div className="cmd-panel" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmd-input"
          placeholder="跳转到页面，或输入股票代码（如 600519）…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="cmd-list">
          {list.length === 0 && <div className="cmd-empty">无匹配</div>}
          {list.map((it, i) => (
            <div key={it.key + i}
              className={`cmd-item ${i === idx ? "active" : ""}`}
              onMouseEnter={() => setIdx(i)}
              onClick={() => choose(it)}>
              <span className="cmd-label">{it.label}</span>
              <span className="cmd-group">{it.group}</span>
            </div>
          ))}
        </div>
        <div className="cmd-hint">快捷键：Ctrl/Cmd+K 打开 · Alt+1…9 切分组 · F1 帮助 · Esc 关闭</div>
      </div>
    </div>
  );
}
