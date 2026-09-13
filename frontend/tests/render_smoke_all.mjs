// T22 全页面冒烟：逐页导航 + 首帧渲染 + console/pageerror 收集。
// 运行（需先起后端，静态目录已构建前端）：
//   cd frontend && node tests/render_smoke_all.mjs
// 端口默认 21118，可用 DEV_PORT 覆盖；或直接用 tests/_run_smoke.sh 一键编排。
import puppeteer from "puppeteer-core";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = process.env.DEV_PORT || "21118";
const URL = `http://127.0.0.1:${PORT}/`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 当前注册页 key（v2 精简后 17 页，来自 pagesRegistry.jsx 的 PAGES/PAGE_TREE）。
// 旧清单里的 boards/etfs/strategies/paper/limitup 等已被 KEY_ALIAS 归一或移除，
// 逐个 nav 它们只会落到 dashboard，测不出真实页面 —— 故与注册表对齐。
const PAGES_LIST = [
  // 行情（6）
  "quoteboard", "quote", "mktstructure", "sector_radar", "moneyflow", "deal_feed",
  // 研究（2）
  "screen", "factor_hub",
  // 交易（3）
  "trade", "algo", "watchlist",
  // 系统（6）
  "dashboard", "brokers", "accounts", "sysstatus", "system_log", "settings",
];

const EXPECT = {
  dashboard: "仪表盘", quote: "行情分析", quoteboard: "报价牌",
  mktstructure: "市场结构", sector_radar: "板块雷达", moneyflow: "资金流",
  deal_feed: "成交明细", screen: "条件选股", factor_hub: "因子研究",
  trade: "手动交易", algo: "算法交易", watchlist: "自选股",
  brokers: "连接管理", accounts: "多账户网格", sysstatus: "系统状态",
  system_log: "系统日志", settings: "设置",
};

const errors = [];
let pass = 0, fail = 0;
function ok(cond, label, extra = "") {
  if (cond) { pass++; console.log(`[OK  ] ${label} ${extra}`); }
  else { fail++; console.log(`[FAIL] ${label} ${extra}`); }
}

// ============================================================================
// 断言口径说明（本测试的正确性基石，改动前务必先读）
//
// 1) 导航必须走 window.__qmtNavTo（nav.js 测试桥）。
//    早期用 window.dispatchEvent(new CustomEvent("nav", ...)) 是**静默失效**的：
//    全站导航走模块内 eventBus（lib/eventBus.js），window 上没有任何 "nav" 监听者
//    唯一监听点是 Workbench 的 onEvent("nav", onNav)。于是页面从不切换，17 页每次
//    采到的都是仪表盘内容，测试却全绿 —— 典型的假通过。
//
// 2) 打开方式必须用 openIn:"tab"（确定性新开并激活）。
//    · "replace" 在 reducer 里只换「当前激活叶子的 params，不换 pageKey」
//      （store/workspace.jsx:371），跨页导航被静默忽略 → 始终停在原页。
//    · "auto" 是用户点菜单的真实路径，但对**单例页面**会「聚焦已存在的 tab」
//      （store/workspace.jsx:392）—— 刚访问过的页会把焦点抢回去，逐页断言不稳定。
//    · 另有硬上限 MAX_TABS=24：一旦到达，openNewTab 直接 return state（不新开、
//      也不激活）。所以本测试**不能无节制堆 tab**：逐页阶段需复用「同页聚焦」，
//      累积切换阶段需先归零。见下 T22b / T22c 的实现。
//
// 3) 「当前前台是哪一页」只能问 store，不能从 DOM 反推。
//    keep-alive(LRU) 下只有激活 tab 的 .wb-tabpane 不带 "inactive"
//    （Workbench.jsx:172），因此「未 inactive 的 pane」恒为 1 个 = 激活 tab；
//    而它的 DOM 位置 = state.tabs 的下标（aliveTabs 直接 map 而来），
//    **与「最新使用的 tab」无关** —— 这正是历史上「取最后一个可见 pane」断言
//    指错（期望「设置」实得「行情分析」）的根因。
//    Provider 已通过 window.__qmtWorkspace 暴露权威快照，本测试一律以它为准：
//    activeTitle / activePageKey 是断言「切到了哪一页」的唯一权威来源。
// ============================================================================

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: "new",
  args: ["--no-first-run", "--disable-extensions",
    "--user-data-dir=" + process.env.TEMP + "\\edge_all_smoke"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000 });
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

// 清掉持久化布局/自动分屏状态，让每次运行从同一初始工作区出发。
// 否则上一轮残留的 tab 树 / autoSplit 会影响 tab 累积与 LRU，结果不可复现。
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });
await page.evaluate(() => { try { localStorage.clear(); } catch { /* noop */ } });
await page.reload({ waitUntil: "networkidle2", timeout: 30000 });
await sleep(1500);

// store 权威快照（window.__qmtWorkspace 由 WorkspaceProvider 维护）。
const snap = () => page.evaluate(() => window.__qmtWorkspace || null);
// 激活页的正文采样（用于「有内容」判定；取自 DOM 是合适的，因为这只问渲染结果）。
const bodySample = () => page.evaluate(() => {
  const vis = Array.from(document.querySelectorAll(".wb-tabpane"))
    .find((p) => !p.classList.contains("inactive"));
  const leaf = vis ? (vis.querySelector(".pane-leaf.active") || vis.querySelector(".pane-leaf")) : null;
  const body = leaf ? leaf.querySelector(".pane-leaf-body") : null;
  return body ? body.innerText : "";
});

try {
  // 首屏（dashboard）
  const dash = await page.evaluate(() => {
    const wb = document.querySelector(".workbench, .wb-layout, #root");
    return { ok: !!wb, hasNav: document.querySelectorAll(".wb-tab, .menu-bar, .function-tree").length > 0 };
  });
  ok(dash.ok && dash.hasNav, "T22a 首屏加载（root/导航存在）");
  const s0 = await snap();
  ok(!!s0, "T22a2 store 测试桥可用（window.__qmtWorkspace）",
    s0 ? `激活「${s0.activeTitle}」 tabs=${s0.tabCount}` : "!! 未暴露，后续断言不可信");

  // ---- T22b：逐页导航 + 渲染 + 标题 ----
  //
  // 打开方式用 "auto"：对已访问过的单例页会**聚焦已有 tab**（恰好复用，不新增），
  // 避免撞上 MAX_TABS=24 后「点了没反应」。首轮每页首次点击会新开 tab，
  // 17 页 < 24，安全；若某页已存在则复用。这同时覆盖了用户真实点菜单的路径。
  for (const key of PAGES_LIST) {
    const label = EXPECT[key] || key;
    const dispatched = await page.evaluate((k) => {
      if (typeof window.__qmtNavTo !== "function") return false;
      return window.__qmtNavTo(k, { params: {}, openIn: "auto" }) === true;
    }, key);
    await sleep(1600);
    const st = await snap();
    const sample = await bodySample();
    const rendered = sample.replace(/[\s|]/g, "").length > 6;
    // 标题必须等于该页 label —— 最强的「确实切到了这一页」断言。
    const want = EXPECT[key];
    const titleOk = !want || (st && st.activeTitle === want);
    ok(dispatched && rendered && titleOk,
      `T22b ${label} 页渲染（tabs=${st ? st.tabCount : "?"} 有内容）`,
      !titleOk ? `!! 期望标题「${want}」实得「${st ? st.activeTitle : "(无 store)"}」`
        : `${dispatched ? "" : "navTo 未就绪 "}${st.activeTitle} / ${sample.replace(/\s+/g, " ").slice(0, 40)}`);
  }

  // ---- T22c：快速交叉切换稳定性 ----
  //
  // 关键：先把工作区归零到单 tab（CLOSE_OTHERS 的等价路径 —— 这里用重新加载 +
  // 清持久化不可行，会丢 store 桥；改为逐个关闭多余 tab 成本过高）。
  // 实际上 MAX_TABS=24 是硬上限，若 T22b 已累积较多 tab，后续 openIn:"tab" 可能被
  // 静默忽略。故这里统一用 "auto"（单例页聚焦已有、多实例页新开），
  // 断言只看 store 的 activeTitle —— 与打开方式无关，始终确定性。
  const shuffle = ["quote", "quoteboard", "screen", "trade", "settings"];
  for (let round = 0; round < 2; round++) {
    for (const key of shuffle) {
      await page.evaluate((k) => window.__qmtNavTo && window.__qmtNavTo(k, { openIn: "auto" }), key);
      await sleep(600);
    }
  }
  await page.evaluate(() => window.__qmtNavTo("settings", { openIn: "auto" }));
  await sleep(1500);
  const sf = await snap();
  const finalSample = await bodySample();
  const switched = !!sf && sf.activeTitle === "设置";
  const hasContent = finalSample.replace(/\s|/g, "").length > 6;
  ok(switched && hasContent, "T22c 快速切换 5 页 × 2 轮且最终切页生效",
    `${switched ? "切至" : "!! 期望「设置」实得"}「${sf ? sf.activeTitle : "(无 store)"}」`
    + ` | tabs=${sf ? sf.tabCount : "?"}`
    + ` | 分布=${sf ? sf.tabs.map((t) => (t.active ? "*" : "") + t.title).join(",") : "?"}`);

} catch (e) {
  fail++;
  console.log("[EXCEPTION]", e.message);
} finally {
  await browser.close();
}

console.log("\n==== console/pageerror 收集 ====");
if (errors.length === 0) console.log("(无错误)");
else errors.slice(0, 15).forEach((e) => console.log("  " + e.slice(0, 200)));
console.log(`\n结果: ${pass} pass / ${fail} fail`);
process.exit(fail > 0 ? 1 : 0);
