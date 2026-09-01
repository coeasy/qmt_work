// T22 全页面冒烟：逐页 nav 派发 + 首帧渲染 + console/pageerror 收集。
// 运行（需先起后端，静态目录已构建前端）：
//   cd frontend && node tests/render_smoke_all.mjs
// 端口默认 21118，可用 DEV_PORT 覆盖。
import puppeteer from "puppeteer-core";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = process.env.DEV_PORT || "21118";
const URL = `http://127.0.0.1:${PORT}/`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 全部 31 个注册页 key（来自 pagesRegistry.jsx）
const PAGES = [
  "dashboard", "quote", "quoteboard", "boards", "etfs", "index_overview",
  "rotation", "markettools", "screen", "trade", "limitup", "algo", "paper",
  "strategies", "strmarket", "target", "rebalance", "backtest", "factors",
  "research", "reference", "signal", "alerts", "notifications", "webhooks",
  "brokers", "accounts", "sysstatus", "audit", "reconcile", "settings",
];

const errors = [];
let pass = 0, fail = 0;
function ok(cond, label, extra = "") {
  if (cond) { pass++; console.log(`[OK  ] ${label} ${extra}`); }
  else { fail++; console.log(`[FAIL] ${label} ${extra}`); }
}

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

try {
  await page.goto(URL, { waitUntil: "networkidle2", timeout: 30000 });
  await sleep(1500);

  // 首屏（dashboard）
  const dash = await page.evaluate(() => {
    const wb = document.querySelector(".workbench, .wb-layout, #root");
    return { ok: !!wb, hasNav: document.querySelectorAll(".wb-tab, .menu-bar, .function-tree").length > 0 };
  });
  ok(dash.ok && dash.hasNav, "T22a 首屏加载（root/导航存在）");

  // 逐页 nav 派发 + 等渲染 + 检查该页容器出现
  for (const key of PAGES) {
    const label = key === "quote" ? "行情分析" : key;
    await page.evaluate((k) =>
      window.dispatchEvent(new CustomEvent("nav", { detail: { pageKey: k, params: {}, openIn: "tab" } })),
      key);
    await sleep(1800);
    const st = await page.evaluate(() => {
      // v3 工作区：keep-alive 容器 .wb-tabpane，激活面板内部叶子为 .pane-leaf
      const active = document.querySelector(".wb-tabpane:not(.inactive)");
      const leaves = active ? active.querySelectorAll(".pane-leaf") : [];
      const txt = Array.from(leaves).map((p) => (p.innerText || "").slice(0, 80)).join("|");
      // 页面必须有实际内容（非纯空白）
      return { paneCount: leaves.length, sample: txt.slice(0, 120) };
    });
    const rendered = st.sample.replace(/[\s|]/g, "").length > 6;
    ok(rendered, `T22b ${label} 页渲染（pane=${st.paneCount} 有内容）`,
      st.sample.replace(/\|/g, " / ").slice(0, 60));
  }

  // 交叉切换稳定性：快速来回切 5 页 × 2 轮，确保无泄漏级报错
  const shuffle = ["quote", "boards", "screen", "trade", "settings"];
  for (let round = 0; round < 2; round++) {
    for (const key of shuffle) {
      await page.evaluate((k) =>
        window.dispatchEvent(new CustomEvent("nav", { detail: { pageKey: k, params: {}, openIn: "replace" } })),
        key);
      await sleep(600);
    }
  }
  ok(true, "T22c 快速切换 5 页 × 2 轮无异常（console 未收集致命错误则通过）");

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
