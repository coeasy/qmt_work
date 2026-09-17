import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require_ = createRequire(import.meta.url);
const { escHtml, loadingPageUrl } = require_("../electron/loadingPage.cjs") as {
  escHtml: (s: unknown) => string;
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
 */

/** 从 data URL 还原出 HTML —— 断言可读的 HTML，而不是一长串百分号编码 */
function html(url: string): string {
  const prefix = "data:text/html;charset=utf-8,";
  expect(url.startsWith(prefix)).toBe(true);
  return decodeURIComponent(url.slice(prefix.length));
}

describe("loadingPageUrl · 启动中", () => {
  it("显示品牌与进度指示，且**不**出现重试按钮", () => {
    const h = html(loadingPageUrl("booting", "正在启动后端服务，请稍候…"));
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

describe("loadingPageUrl · 启动失败", () => {
  const h = html(loadingPageUrl("failed", "后端服务启动失败，无法进入主界面。", "后端进程已退出（code=2）"));

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
    // （data: 不匹配 isLocalOrigin），但不能因此养成依赖内联的写法。
    expect(h).toContain("addEventListener");
    expect(h).toContain("electronAPI.bootRetry");
    expect(h).not.toMatch(/\son[a-z]+\s*=/i);
  });

  it("没有 error 时用 message 兜底（不能出现空的红色框）", () => {
    const h2 = html(loadingPageUrl("failed", "后端服务启动失败，无法进入主界面。"));
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
    const h = html(loadingPageUrl("failed", "启动失败", '<script>alert(1)</script>'));
    expect(h).not.toContain("<script>alert(1)</script>");
    expect(h).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("null / undefined 不会渲染出 'null' 字样", () => {
    expect(escHtml(null)).toBe("");
    expect(escHtml(undefined)).toBe("");
  });
});
