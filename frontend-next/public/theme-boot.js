/* 主题预热（防闪烁）——必须在样式与 React 之前**同步**执行。
 *
 * 为什么是独立文件而不是 <head> 里的内联脚本：
 * 桌面壳给本机源下发了 CSP `script-src 'self'`，**内联脚本会被直接拦掉**，
 * 预热形同虚设（浅色系统下会出现「先深色闪一下再变浅色」）。
 * 独立同源文件同时满足 CSP 与「同步执行」两个要求。
 *
 * 规则必须与 src/stores/ui.ts 的 load()/apply() 保持一致，改一处要同步另一处：
 *   themePref: auto -> 跟随系统 prefers-color-scheme；dark/light -> 直接用
 *   skin:      预设皮肤靠 <html data-skin> 生效（design/skins.css）
 *   updown:    涨跌色独立于主题
 *   customTokens: 自定义背景色**派生好的**令牌（由 ui.ts 计算并落盘）。
 *                 这里只做搬运，不重复实现派生逻辑 —— 单一真源在 design/skins.ts。
 */
(function () {
  try {
    // 首次启动（无持久化记录）默认**深色 + 极夜黑纯黑背景**，与 src/stores/ui.ts 的
    // load() fallback 保持一致。改这里记得同步那边。
    // 主场景是盯盘：深底整体亮度低，长时间看盘更省眼；且这里先写、React 才挂载，
    // 首屏不会出现「先闪一下浅色再变黑」。
    var DEFAULT_SKIN = "midnight";
    // 旧 id（第三方软件名）→ 中性 id。旧版用户把皮肤 id 落在了 localStorage 与服务器
    // 外观配置里，升级后必须仍然生效 —— 用户看不见这串字符串，却不该为零件的改名买单。
    var LEGACY_SKIN = { tongdaxin: "midnight", dazhihui: "graphite", ths: "obsidian" };
    var el = document.documentElement;
    var raw = localStorage.getItem("qmt.ui.v1");
    var p = raw ? JSON.parse(raw) || {} : {};

    var pref = p.themePref;
    if (pref !== "auto" && pref !== "dark" && pref !== "light") {
      // 老版本只存了已解析的 theme；非法 / 缺失则回退到新默认（深色），而非 auto
      pref = p.theme === "dark" || p.theme === "light" ? p.theme : "dark";
    }
    var theme =
      pref === "auto"
        ? window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : pref;

    el.dataset.theme = theme;
    el.dataset.themePref = pref;

    if (typeof p.skin === "string" && p.skin) {
      // 迁移只在**出口**做一次：写进 DOM 的永远是当前有效 id，
      // 旧的持久值无需改写（下次仍会走到这里，幂等）。
      el.dataset.skin = LEGACY_SKIN[p.skin] || p.skin;
    } else if (!raw) {
      el.dataset.skin = DEFAULT_SKIN; // 首次启动默认极夜黑（纯黑）
    }
    if (p.updown === "green-up") el.dataset.updown = "green-up";

    if (p.skin === "custom" && p.customTokens && typeof p.customTokens === "object") {
      for (var k in p.customTokens) {
        if (Object.prototype.hasOwnProperty.call(p.customTokens, k)) {
          el.style.setProperty(k, p.customTokens[k]);
        }
      }
    }
  } catch (e) {
    /* localStorage 不可用（隐私模式等）：退回 :root 的深色令牌 */
  }
})();
