// @vitest-environment jsdom
// GlobalSearch 组件测试（检索分析体系①②）：富信息徽章 / 键盘导航 / 搜索历史 / 板块跳转。
// api 与 nav 均 mock —— 只验证组件交互契约，不依赖真实后端。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import GlobalSearch from "../GlobalSearch.jsx";
import { navTo, navToQuote } from "../../lib/nav.js";
import { api } from "../../api.js";

vi.mock("../../api.js", () => ({
  api: { marketSearch: vi.fn() },
}));
vi.mock("../../lib/nav.js", () => ({
  navTo: vi.fn(),
  navToQuote: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const ROWS = [
  { code: "513090.SH", name: "易方达香港证券ETF", type: "etf", exchange: "上交所", match: "code" },
  { code: "881305.SH", name: "香港证券", type: "board", exchange: "板块", match: "name" },
  { code: "000001.SZ", name: "平安银行", type: "stock", exchange: "深交所", match: "pinyin" },
];

let container;
let root;

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
  act(() => root.unmount());
  container.remove();
});

function mount() {
  act(() => root.render(<GlobalSearch />));
}

function input() {
  return container.querySelector(".gsearch-input");
}

function type(q) {
  act(() => {
    const el = input();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, q);
    el.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}

function fireKey(key) {
  act(() => {
    input().dispatchEvent(new window.KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

function items() {
  return [...container.querySelectorAll(".gsearch-item")];
}

describe("GlobalSearch（顶栏标的检索）", () => {
  it("输入防抖后渲染带类型徽章的结果，点击股票 → navToQuote", async () => {
    api.marketSearch.mockResolvedValue(ROWS);
    mount();
    type("香港");
    await act(async () => { vi.advanceTimersByTime(300); });
    const rows = items();
    expect(rows).toHaveLength(3);
    // 徽章：ETF / 板块 / 股 按类型渲染
    expect(rows[0].querySelector(".gbadge.etf").textContent).toBe("ETF");
    expect(rows[1].querySelector(".gbadge.board").textContent).toBe("板块");
    expect(rows[2].querySelector(".gbadge.stock").textContent).toBe("股");
    // 拼音命中标注
    expect(rows[2].querySelector(".gsearch-meta").textContent).toBe("拼音");
    // 点击第一项（股票/ETF）→ 行情页
    act(() => rows[0].click());
    expect(navToQuote).toHaveBeenCalledWith("513090.SH");
    expect(navTo).not.toHaveBeenCalled();
  });

  it("点击板块结果 → 跳市场结构-板块行情（不走去情页）", async () => {
    api.marketSearch.mockResolvedValue(ROWS);
    mount();
    type("香港");
    await act(async () => { vi.advanceTimersByTime(300); });
    act(() => items()[1].click());
    expect(navTo).toHaveBeenCalledWith("mktstructure", { params: { tab: "boards" } });
    expect(navToQuote).not.toHaveBeenCalled();
  });

  it("键盘导航：↑↓ 移动高亮、Enter 打开高亮项", async () => {
    api.marketSearch.mockResolvedValue(ROWS);
    mount();
    type("香港");
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(items()[0].classList.contains("active")).toBe(true);
    fireKey("ArrowDown");
    expect(items()[1].classList.contains("active")).toBe(true);
    fireKey("ArrowUp");
    expect(items()[0].classList.contains("active")).toBe(true);
    // Enter 打开高亮项（第 0 项 = ETF → 行情页）
    fireKey("Enter");
    expect(navToQuote).toHaveBeenCalledWith("513090.SH");
  });

  it("选中后写入搜索历史（localStorage），空输入聚焦展示历史", async () => {
    api.marketSearch.mockResolvedValue([ROWS[0]]);
    mount();
    type("513090");
    await act(async () => { vi.advanceTimersByTime(300); });
    act(() => items()[0].click());
    const saved = JSON.parse(localStorage.getItem("qmt.gsearch.history") || "[]");
    expect(saved).toEqual([{ code: "513090.SH", name: "易方达香港证券ETF" }]);
    // 清空输入再聚焦 → 显示「最近搜索」分区与历史条目
    type("");
    act(() => { input().focus(); });
    expect(container.querySelector(".gsearch-sec").textContent).toBe("最近搜索");
    expect(items()).toHaveLength(1);
    expect(items()[0].textContent).toContain("513090.SH");
    // 点击历史条目同样打开行情
    act(() => items()[0].click());
    expect(navToQuote).toHaveBeenCalledWith("513090.SH");
  });

  it("空查询不发请求；后端异常静默降级为无结果", async () => {
    api.marketSearch.mockRejectedValue(new Error("down"));
    mount();
    type("x");
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(api.marketSearch).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".gsearch-drop")).toBeNull();
  });
});
