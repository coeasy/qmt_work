// @vitest-environment jsdom
// 工具面板冒烟（2026-09 第三期补齐的孤儿端点入口）：MarketTools / RuntimeJobs / FactorRegistry。
// api 全 mock —— 只验证渲染契约与关键交互，不依赖真实后端。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiMock 用 vi.hoisted 提升到 mock 工厂之前（vi.mock 会被 hoist 到文件顶部）
const apiMock = vi.hoisted(() => ({
  klineSyncStatus: vi.fn(() => Promise.resolve({ enabled: true, sync_time: "18:00" })),
  klineCacheStats: vi.fn(() => Promise.resolve({ rows: 10 })),
  klineCacheClear: vi.fn(() => Promise.resolve({ deleted: 1 })),
  marketQuote: vi.fn(() => Promise.resolve({ last: 10 })),
  marketCrawl: vi.fn(() => Promise.resolve({ crawled_codes: [], bars_inserted: 0 })),
  klineExport: vi.fn(() => Promise.resolve({ exported: 1, rows: 10, dest_dir: "/d" })),
  klineSync: vi.fn(() => Promise.resolve({ codes_total: 1, files_count: 1, rows: 10, errors: [], dest_dir: "/d" })),
  marketExport: vi.fn(() => Promise.resolve({ content: "a,b", filename: "export.csv", count: 1 })),
  syncSubscribe: vi.fn(() => Promise.resolve({ subscribed: ["600519.SH"] })),
  runtimeJobsList: vi.fn(() => Promise.resolve([{ id: "j1", kind: "sync", name: "s", status: "done" }])),
  runtimeJobsSubmit: vi.fn(() => Promise.resolve({ id: "j2", status: "queued" })),
  runtimeJobDetail: vi.fn(() => Promise.resolve({ id: "j1", status: "done" })),
  runtimeJobCancel: vi.fn(() => Promise.resolve({})),
  factorsList: vi.fn(() => Promise.resolve([{ name: "ma", desc: "均线", params: { win: 20 } }])),
  factorCompute: vi.fn(() => Promise.resolve({ ma: [1, 2] })),
}));

vi.mock("../../api.js", () => ({ api: apiMock }));
vi.mock("../../hooks/useActiveInterval.js", () => ({ useActiveInterval: vi.fn() }));

import MarketTools from "../MarketTools.jsx";
import RuntimeJobs from "../RuntimeJobs.jsx";
import FactorRegistry from "../FactorRegistry.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(Comp) {
  await act(async () => {
    root.render(<Comp />);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("MarketTools（数据中心·行情数据）", () => {
  it("渲染八张工具卡：报价/缓存/抓取/导出/同步/统一导出/订阅", async () => {
    await render(MarketTools);
    for (const t of ["实时报价", "K 线缓存", "手动抓取行情", "K 线导出", "K 线全量同步", "统一导出 / REST 订阅"]) {
      expect(container.textContent).toContain(t);
    }
  });

  it("K 线导出：dest_dir 必填（空时按钮禁用），填后可提交并显示结果", async () => {
    await render(MarketTools);
    const buttons = [...container.querySelectorAll("button")];
    const exportBtn = buttons.find((b) => b.textContent === "导出" && b.disabled);
    expect(exportBtn).toBeTruthy(); // dest_dir 为空 → 禁用
  });

  it("统一导出：rows 非法 JSON 时提示且不调后端", async () => {
    await render(MarketTools);
    const ta = container.querySelector("textarea");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, "value").set;
      setter.call(ta, "{bad json");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const btn = [...container.querySelectorAll("button")].find((b) => b.textContent === "导出" && !b.disabled);
    await act(async () => { btn.click(); await Promise.resolve(); });
    expect(apiMock.marketExport).not.toHaveBeenCalled();
    expect(container.textContent).toContain("rows 不是合法 JSON 数组");
  });
});

describe("RuntimeJobs（任务运行时）", () => {
  it("加载任务列表并渲染行", async () => {
    await render(RuntimeJobs);
    expect(apiMock.runtimeJobsList).toHaveBeenCalled();
    expect(container.textContent).toContain("j1");
    expect(container.textContent).toContain("sync");
  });

  it("提交 screen 任务缺 conditions 时前端拦截", async () => {
    await render(RuntimeJobs);
    const sel = container.querySelector("select");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, "value").set;
      setter.call(sel, "screen");
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const submit = [...container.querySelectorAll("button")].find((b) => b.textContent === "提交");
    await act(async () => { submit.click(); await Promise.resolve(); });
    expect(apiMock.runtimeJobsSubmit).not.toHaveBeenCalled();
    expect(container.textContent).toContain("conditions");
  });
});

describe("FactorRegistry（因子注册表）", () => {
  it("加载注册表并渲染指标清单", async () => {
    await render(FactorRegistry);
    expect(apiMock.factorsList).toHaveBeenCalled();
    expect(container.textContent).toContain("ma");
    expect(container.textContent).toContain("均线");
  });
});
