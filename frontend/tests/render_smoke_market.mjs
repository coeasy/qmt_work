// S12–S15 浏览器冒烟：指数条 / 板块榜 / ETF（详情+筛选）/ 摘要卡+同板块联动
// 运行（需先起后端，其静态目录已构建前端）：
//   cd frontend && node tests/render_smoke_market.mjs
// 端口默认 21118（即后端静态服务），可用 DEV_PORT 覆盖。
import puppeteer from "puppeteer-core";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = process.env.DEV_PORT || "21118";
const URL = `http://127.0.0.1:${PORT}/`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const errors = [];
let pass = 0, fail = 0;
function ok(cond, label, extra = "") {
  if (cond) { pass++; console.log(`[OK  ] ${label} ${extra}`); }
  else { fail++; console.log(`[FAIL] ${label} ${extra}`); }
}

// 轮询直到页面条件满足（数据源偶发慢，避免固定 sleep 误杀）
async function waitFor(page, fn, timeout = 30000, interval = 500) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await page.evaluate(fn)) return true;
    await sleep(interval);
  }
  return false;
}

async function nav(page, detail) {
  await page.evaluate((d) =>
    window.dispatchEvent(new CustomEvent("nav", { detail: d })), detail);
  await sleep(2500);
}

// 多 tab 场景下显式激活目标 tab（贴近真实用户点击），避免查到非激活（未渲染）pane
async function activateTab(page, label) {
  return page.evaluate((lbl) => {
    const tab = [...document.querySelectorAll(".wb-tab")]
      .find((t) => t.textContent.includes(lbl));
    if (tab) { tab.click(); return true; }
    return false;
  }, label);
}

async function clickTabByText(page, text) {
  return page.evaluate((t) => {
    const btn = [...document.querySelectorAll(".mp-drawer-tab")]
      .find((b) => b.textContent.includes(t));
    if (btn) { btn.click(); return true; }
    return false;
  }, text);
}

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: "new",
  args: ["--no-first-run", "--disable-extensions",
    "--user-data-dir=" + process.env.TEMP + "\\edge_market_smoke"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000 });
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

try {
  await page.goto(URL, { waitUntil: "networkidle2", timeout: 30000 });
  await sleep(2000);

  // S12 指数条（顶部全局 ticker，E2）
  const ticker = await page.evaluate(() => {
    const g = document.querySelector(".ticker-group");
    if (!g) return { ok: false };
    return {
      ok: true,
      names: g.querySelectorAll(".ticker-name").length,
      vals: g.querySelectorAll(".ticker-val").length,
    };
  });
  ok(ticker.ok && ticker.names > 0 && ticker.vals > 0,
    "S12 指数条渲染（ticker-name/val 非空）",
    `names=${ticker.names} vals=${ticker.vals}`);

  // S13 板块榜（Boards 页）
  await nav(page, "boards");
  await activateTab(page, "板块行情");
  await sleep(500);
  const boardOk = await waitFor(page, () =>
    document.querySelectorAll(".bd-list tbody tr").length > 0);
  const board = await page.evaluate(() => {
    const list = document.querySelector(".bd-list");
    const rows = list ? list.querySelectorAll("tbody tr").length : 0;
    const txt = (list?.innerText || "").trim();
    return { rows, hasBoard: /行业|概念|板块/.test(txt) };
  });
  ok(boardOk && board.rows > 0, "S13 板块榜渲染（tbody 行 > 0）", `rows=${board.rows}`);
  ok(board.hasBoard, "S13 板块榜含板块分组文案");

  // S14 ETF（清单 + H3 筛选 + H2 详情）
  await nav(page, "etfs");
  await activateTab(page, "ETF 基金");
  await sleep(500);
  const etfRowsOk = await waitFor(page, () =>
    document.querySelectorAll(".etf-body tbody tr.bd-stock").length > 0);
  const etf = await page.evaluate(() => {
    const range = document.querySelector(".etf-range");
    const body = document.querySelector(".etf-body");
    const rows = document.querySelectorAll(".etf-body tbody tr.bd-stock").length;
    return { hasRange: !!range, hasBody: !!body, rows };
  });
  ok(etf.hasBody, "S14a ETF 清单容器 .etf-body 存在");
  ok(etf.hasRange, "S14b H3 涨跌幅区间筛选 .etf-range 存在");
  ok(etfRowsOk && etf.rows > 0, "S14c ETF 行渲染（tr.bd-stock > 0）", `rows=${etf.rows}`);

  // 点击首行 → H2 详情抽屉
  await page.evaluate(() => {
    const row = document.querySelector(".etf-body tbody tr.bd-stock");
    if (row) row.click();
  });
  const detailOk = await waitFor(page, () => {
    const d = document.querySelector(".etf-detail");
    return d && (d.innerText || "").trim().length > 0;
  });
  ok(detailOk, "S14d H2 ETF 详情抽屉 .etf-detail 打开且有内容");

  // S15 摘要卡 + 同板块联动（行情分析页）
  await nav(page, { pageKey: "quote", params: { code: "600519.SH" } });
  await activateTab(page, "行情分析");
  await sleep(500);
  const openedMf = await clickTabByText(page, "多维摘要");
  await sleep(2000);
  const mfOk = await waitFor(page, () => !!document.querySelector(".mf-card"));
  const mf = await page.evaluate(() => {
    const c = document.querySelector(".mf-card");
    return { ok: !!c, text: c ? (c.innerText || "").trim().length : 0 };
  });
  ok(openedMf && mfOk, "S15a 多维摘要卡 .mf-card 渲染", `textLen=${mf.text}`);

  // 切换到同板块联动
  const openedLink = await clickTabByText(page, "同板块联动");
  await sleep(4000);
  const link = await page.evaluate(() => {
    const c = document.querySelector(".link-card");
    if (!c) return { ok: false };
    const txt = (c.innerText || "");
    return { ok: true, hasCode: /\.SH|\.SZ/.test(txt) };
  });
  ok(openedLink && link.ok, "S15b 同板块联动卡 .link-card 渲染");
  ok(link.hasCode, "S15c 联动列出成分股代码（真实数据）");

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
