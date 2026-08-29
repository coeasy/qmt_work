// 键盘流：全局快捷键（通达信风格 F-key + 通用键位）
//  - Ctrl/Cmd+K：命令面板
//  - Alt+1..9：跳转到第 N 个功能分组的首个页面
//  - F1：帮助/快捷键说明
//  - Esc：关闭浮层
//  - F3 上证指数 / F4 深证成指 / F5 分时↔K线 / F6 自选股 / F10 F10 资料 / F12 交易
import { useEffect } from "react";
import { PAGE_TREE } from "../pagesRegistry.jsx";
import { navTo, navToQuote } from "../lib/nav.js";

export function useHotkeys() {
  useEffect(() => {
    const onKey = (e) => {
      // 输入框内仅保留 Esc 关闭浮层（其余交给 input）
      const tag = (e.target?.tagName || "").toLowerCase();
      const inEditable = tag === "input" || tag === "textarea" || e.target?.isContentEditable;

      const mod = e.ctrlKey || e.metaKey;
      if (mod && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("cmd:toggle"));
        return;
      }
      if (mod && (e.key === "b" || e.key === "B")) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("tree:toggle"));
        return;
      }
      if (e.key === "Escape") {
        window.dispatchEvent(new CustomEvent("cmd:close"));
        window.dispatchEvent(new CustomEvent("help:close"));
        return;
      }
      if (e.altKey && /^[1-9]$/.test(e.key)) {
        const g = PAGE_TREE[Number(e.key) - 1];
        if (g && g.items[0]) {
          e.preventDefault();
          window.dispatchEvent(new CustomEvent("nav", { detail: g.items[0].key }));
        }
        return;
      }

      // F3~F12 仅在非输入态下生效
      if (inEditable) return;
      const k = e.key;
      if (k === "F1") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("help:toggle"));
        return;
      }
      if (k === "F3") {
        e.preventDefault();
        // v3：nav 协议携带参数直达（旧版写 sessionStorage，行情 tab 已开时点了没反应）
        navToQuote("000001.SH");
        return;
      }
      if (k === "F4") {
        e.preventDefault();
        navToQuote("399001.SZ");
        return;
      }
      if (k === "F5") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("sa:toggle-period"));
        return;
      }
      if (k === "F6") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("nav", { detail: "quoteboard" }));
        // 综合排名 = 自选风格入口
        window.dispatchEvent(new CustomEvent("qb:switch", { detail: "watch" }));
        return;
      }
      if (k === "F10") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("sa:focus-f10"));
        return;
      }
      if (k === "F12") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("nav", { detail: "trade" }));
        return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
