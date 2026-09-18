// 启动期加载页（booting / failed 两态）的唯一实现。
//
// 抽成独立模块的原因有两条：
//   ① 它必须是**可单测**的 —— 「失败页到底有没有把中文原因和重试按钮渲染出来」
//      不能只靠「窗口标题变了」推断。放在 main.cjs 里则必须启动整个 Electron 才能测，
//      实际结果就是没人测（本模块出现前，失败页只在人肉截图里被看过一次）。
//   ② 纯函数、零 Electron 依赖 —— 参数进、HTML 出，可以脱离主进程直接渲染验证。
//
// ★★ 关键约束：加载页**绝不能以 data: 或 file:// 的形式作为窗口的首屏导航**。
//    实测（output/region_repro 的 REPRO_BOOT 二分；最终页统一为 http://127.0.0.1:21401/，
//    同一页面、同一测量方法；判据 = 标题栏 WM_NCHITTEST，2=HTCAPTION 可拖 / 1=HTCLIENT 拖不动）：
//      · 直接加载目标页          → 2（可拖）
//      · 先 data: 再导航到目标   → 恒为 1，**永久不可恢复**
//      · 先 file:// 再导航到目标 → 恒为 1（同样不可恢复）
//      · 先 about:blank 再导航   → 2（可拖）
//      导航后强制 resize / reload 均无效；注入全新 `-webkit-app-region: drag`
//      元素同样无效 ⇒ 与页面 CSS/DOM 无关，是**窗口级**状态被写坏：
//      Chromium 不再把 draggable region 上报给窗口过程。
//    ⚠️ 早期只测到「file:// 首屏」在**最终页也是 file://** 时正常，据此误判过一次 ——
//       真正的分界是「最终页为 http 的跨协议导航」，务必以最终页为 http 的矩阵为准。
//    ⇒ 这就是「客户端窗口不能拖动」的根因（R10.1 引入 data: 加载页时踩到）。
//      main.cjs 的 showLoading() 因此改为「先 about:blank，再把本函数产出的 HTML
//      用 document.write 注入」；下面的 loadingPageUrl() 只剩一个用途：
//      给 output/render_probe 这类**单次渲染**探针用（那里不存在后续 http 导航，
//      因此不受副作用影响）。**产品代码不得使用它。**
//
// 为什么加载页里可以用内联 <style>/<script>：applySecurityPolicy 只对
// isLocalOrigin(url) 下发 CSP，file:// 与 data: 都不匹配 ⇒ 内联可用
// （应用页面仍禁止内联脚本，主题预热因此抽成同源文件 public/theme-boot.js）。
// 页面里不使用任何内联事件属性（onclick=…），一律 addEventListener，
// 这样将来若把 CSP 收紧到加载页也不会连带失效。

"use strict";

function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function loadingPageHtml(phase, message, error) {
  const failed = phase === "failed";
  // 标题随阶段变：任务栏/托盘里也能一眼看出是「还在启动」还是「起不来」，
  // 而不是两种状态共用一个「正在启动」。
  const title = failed ? "qmt_work · 启动失败" : "qmt_work · 正在启动";
  // 阅读顺序：品牌 → 一句「发生了什么」 → 具体原因 → 出路（按钮） → 补充提示。
  // 旧版把 msg 放在最后，失败页会出现「先看到按钮和提示、最后才知道发生了什么」的错序。
  const content = failed
    ? `<div class="msg">${escHtml(message)}</div>
  <div class="err" id="errBox">${escHtml(error || message)}</div>
  <button class="btn" id="retryBtn">重新启动后端</button>
  <div class="hint">若反复失败，请检查端口是否被占用，或查看日志 startup-error.log</div>`
    : `<div class="spin"></div>
  <div class="msg">${escHtml(message)}</div>`;
  const html = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>${title}</title>
<style>
  html,body{margin:0;height:100%;background:#0f1420;color:#c9d4e5;overflow:hidden;
    font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
    -webkit-user-select:none;user-select:none}
  .wrap{height:100%;display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:18px;padding:0 40px;box-sizing:border-box}
  .brand{font-size:20px;font-weight:600;letter-spacing:.5px;color:#e8eefb}
  .brand em{font-style:normal;color:#3b82f6}
  .msg{font-size:13px;color:#8b9bb4;text-align:center;line-height:1.7}
  .spin{width:34px;height:34px;border:3px solid #1e293b;border-top-color:#3b82f6;
    border-radius:50%;animation:sp .9s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  .err{margin-top:6px;max-width:640px;max-height:150px;overflow:auto;text-align:left;
    font-size:12px;line-height:1.6;color:#f0a3a3;background:#1b1116;
    border:1px solid #43212a;border-radius:6px;padding:10px 12px;
    white-space:pre-wrap;word-break:break-all;
    -webkit-user-select:text;user-select:text}
  .btn{margin-top:4px;padding:8px 22px;font-size:13px;color:#fff;background:#2563eb;
    border:none;border-radius:6px;cursor:pointer}
  .btn:hover{background:#1d4ed8}
  .btn:disabled{background:#334155;cursor:default}
  .hint{font-size:11px;color:#5b6b83}
</style></head>
<body><div class="wrap">
  <div class="brand">qmt_work <em>·</em> 多券商量化平台</div>
  ${content}
</div>
<script>
var b = document.getElementById("retryBtn");
if (b) {
  b.addEventListener("click", function () {
    b.disabled = true;
    b.textContent = "正在重启…";
    try { window.electronAPI.bootRetry(); } catch (e) { b.disabled = false; b.textContent = "重新启动后端"; }
  });
}
</script>
</body></html>`;
  return html;
}

/**
 * 加载页的 data: URL 形式 —— **仅供 output/render_probe 这类单次渲染探针使用**。
 *
 * 那里加载页是窗口的**唯一**页面（不存在后续 http 导航），所以不会触发下面的副作用。
 * ⚠️ 产品代码（main.cjs）**不得使用**：窗口一旦以 data:（或 file://）作为首屏导航，
 * draggable region 会永久失效，表现为「标题栏拖不动窗口」。正常路径是
 * `loadingPageHtml` + 先 `about:blank` 再 `document.write` 注入。
 */
function loadingPageUrl(phase, message, error) {
  return "data:text/html;charset=utf-8," + encodeURIComponent(loadingPageHtml(phase, message, error));
}

module.exports = { escHtml, loadingPageHtml, loadingPageUrl };
