import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require_ = createRequire(import.meta.url);
const { escHtml, loadingPageHtml, loadingPageUrl } = require_("../electron/loadingPage.cjs") as {
  escHtml: (s: unknown) => string;
  loadingPageHtml: (phase: string, message: string, error?: string) => string;
  loadingPageUrl: (phase: string, message: string, error?: string) => string;
};

/**
 * 启动期加载页（booting / failed）回归测试。
 *
 * 为什么值得测：这两个页面出现在**用户最焦虑的两个时刻**（等 7~30 秒的冷启动、
 * 以及后端起不来）。旧实现失败时是「死窗口 + 英文 startup-error.log」+ 一个
 * 盖住界面的模态框，用户既不知道原因也没有出路。这里把「原因要出现、按钮要在、
 * 文本要转义」钉死 —— 否则某次「顺手改一下文案」就可能把失败页变回一句干巴巴的
 * 「启动失败」，而只有真出故障的人才会发现。
 *
 * ★ 被测函数是 `loadingPageHtml`（返回 HTML 字符串）而**不是** `loadingPageUrl`
 *   —— 后者只是「落盘失败时的兜底」，真正发货的路径是 `loadingPageHtml` 落盘 +
 *   `win.loadFile`。测错函数等于没测（本仓库的老毛病：护栏照抄实现、测的不是发货路径）。
 */

// ⚠️ 用 createRequire().resolve() 取**绝对路径字符串**再读：
// 本环境的 node fs 被 shim 包了一层，readFileSync(new URL(...)) 会报
// `TypeError: The URL must be of scheme file`（vitest 下 import.meta.url 不是纯 file: URL）。
const MAIN_CJS = readFileSync(require_.resolve("../electron/main.cjs"), "utf8");

describe("loadingPageHtml · 启动中", () => {
  it("显示品牌与进度指示，且**不**出现重试按钮", () => {
    const h = loadingPageHtml("booting", "正在启动后端服务，请稍候…");
    expect(h).toContain("<title>qmt_work · 正在启动</title>");
    expect(h).toContain("多券商量化平台");
    expect(h).toContain("正在启动后端服务，请稍候…");
    expect(h).toContain('class="spin"');
    // 判据是**按钮元素**不存在（脚本里始终有 getElementById("retryBtn") 的引用，
    // 那是被 if (b) 兜住的；用 "retryBtn" 当判据会误判）。
    expect(h).not.toContain('id="retryBtn"');
    expect(h).not.toContain('class="btn"');
    expect(h).not.toContain('id="errBox"');
  });
});

describe("loadingPageHtml · 启动失败", () => {
  const h = loadingPageHtml("failed", "后端服务启动失败，无法进入主界面。", "后端进程已退出（code=2）");

  it("标题切到「启动失败」（任务栏里也能区分两态）", () => {
    expect(h).toContain("<title>qmt_work · 启动失败</title>");
  });

  it("★ 必须把具体失败原因显示出来，而不是一句笼统的「启动失败」", () => {
    expect(h).toContain('id="errBox"');
    expect(h).toContain("后端进程已退出（code=2）");
  });

  it("★ 必须给出路：可点击的「重新启动后端」按钮", () => {
    expect(h).toContain('id="retryBtn"');
    expect(h).toContain("重新启动后端");
  });

  it("按钮走 addEventListener 调 electronAPI.bootRetry，而不是内联 onclick", () => {
    // 内联事件属性是 CSP 收紧时第一个失效的东西；加载页现在不受 CSP 约束
    // （file:// / data: 都不匹配 isLocalOrigin），但不能因此养成依赖内联的写法。
    expect(h).toContain("addEventListener");
    expect(h).toContain("electronAPI.bootRetry");
    expect(h).not.toMatch(/\son[a-z]+\s*=/i);
  });

  it("没有 error 时用 message 兜底（不能出现空的红色框）", () => {
    const h2 = loadingPageHtml("failed", "后端服务启动失败，无法进入主界面。");
    expect(h2).toContain("后端服务启动失败，无法进入主界面。");
    expect(h2).toContain('id="errBox"');
  });
});

describe("escHtml · 注入防护", () => {
  it("转义 HTML 元字符（失败原因来自后端/系统，不是可信输入）", () => {
    expect(escHtml('<img src=x onerror=alert(1)>')).toBe("&lt;img src=x onerror=alert(1)&gt;");
    expect(escHtml('a & b "c"')).toBe("a &amp; b &quot;c&quot;");
  });

  it("★ 失败原因里的标签不会变成真标签（不会在加载页里执行）", () => {
    const h = loadingPageHtml("failed", "启动失败", '<script>alert(1)</script>');
    expect(h).not.toContain("<script>alert(1)</script>");
    expect(h).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("null / undefined 不会渲染出 'null' 字样", () => {
    expect(escHtml(null)).toBe("");
    expect(escHtml(undefined)).toBe("");
  });
});

describe("loadingPageUrl · 仅供单次渲染探针，与 HTML 同源", () => {
  it("data: URL 只是 loadingPageHtml 的百分号编码包装（探针不会渲染出另一套页面）", () => {
    const url = loadingPageUrl("failed", "启动失败", "原因");
    const prefix = "data:text/html;charset=utf-8,";
    expect(url.startsWith(prefix)).toBe(true);
    expect(decodeURIComponent(url.slice(prefix.length)))
      .toBe(loadingPageHtml("failed", "启动失败", "原因"));
  });
});

/**
 * ★★ 防回归护栏：「窗口不能拖动」的根因，必须锁死。
 *
 * 实测（output/region_repro 的 REPRO_BOOT 二分；最终页统一为 http，同一测量方法；
 * 判据 = 标题栏 WM_NCHITTEST，2=HTCAPTION 可拖 / 1=HTCLIENT 拖不动）：
 *   直接加载目标页          → 2（可拖）
 *   先 data: 再导航到目标   → 恒为 1，永久不可恢复
 *   先 file:// 再导航到目标 → 恒为 1，同样不可恢复
 *   先 about:blank 再导航   → 2（可拖）
 * ⇒ 首屏导航目标只能是 about:blank（或直接就是应用页）。data: 与 file:// 都会把窗口级
 *   的 draggable region 状态写坏，而 `-webkit-app-region` 的三层 CSS/DOM 证据
 *   （源码 / 构建产物 / CDP 计算值）全都正确 —— 纯前端视角根本看不出问题。
 *   所以这条只能由护栏守，不能靠人记得。
 */
describe("★ 加载页承载方式护栏（data: / file:// 会让窗口永久拖不动）", () => {
  it("首屏导航目标是 about:blank（不是 data: / file://）", () => {
    expect(MAIN_CJS).toContain("loadingPageHtml");
    expect(MAIN_CJS).toMatch(/win\.loadURL\("about:blank"\)/);
  });

  it("产品代码完全不碰 loadingPageUrl / loadFile（那是给渲染探针的）", () => {
    expect(MAIN_CJS).not.toContain("loadingPageUrl");
    expect(MAIN_CJS).not.toContain("win.loadFile(");
  });

  it("createWindow 里不再直接导航加载页（一律经 showLoading）", () => {
    expect(MAIN_CJS).toContain('showLoading("booting"');
  });

  it("注入前有竞态保护：切应用页时必须作废未完成的 document.write", () => {
    // 否则 document.write 晚到会把应用页面整个覆盖掉 —— 比「拖不动」严重得多。
    expect(MAIN_CJS).toContain("loadingGen");
    expect(MAIN_CJS).toContain("loadingGen += 1");
  });
});
