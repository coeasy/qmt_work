import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";

/**
 * 基本面面板的**布局契约**测试 —— 2026-09-21 改为「6 维一排 + 默认收起 + 点击展开」。
 *
 * ## 为什么要锁「默认收起」
 * 这条不是审美偏好，而是**空间契约**：面板挂在工作台底部坞里，坞高
 * `max-height: 42%` 是从 K 线那里借来的（见 `panels.module.css` 的 `.dock`）。
 * 6 块表格全铺开会把 K 线压没 —— 而「6 维有没有数据」这件事本来只需要一行。
 *
 * 所以必须同时锁两件事，缺一不可：
 *   ① 收起时**明细单元格不渲染**（不是靠 CSS 藏起来 —— 那样仍占 DOM 与高度，
 *      且 `textContent` 断言会假通过）；
 *   ② 收起时**仍然看得见每一维的状态**（标题 + 可用性 + 摘要），
 *      否则「默认收起」就退化成「默认什么都不给」。
 *
 * ## 为什么能在这里断言渲染
 * 面板走普通 grid（**不是** DataTable 虚拟表格），jsdom 下正常渲染，
 * 不需要退到源码扫描。
 */
vi.mock("@/services/api/market", async () => {
  const actual = await vi.importActual<typeof import("@/services/api/market")>(
    "@/services/api/market",
  );
  return {
    ...actual,
    marketApi: { ...actual.marketApi, analysis: vi.fn() },
  };
});

// eslint-disable-next-line import/first
import { marketApi } from "@/services/api/market";
// eslint-disable-next-line import/first
import { FundamentalsPanel } from "@/domains/market/panels/FundamentalsPanel";

const api = marketApi as unknown as { analysis: ReturnType<typeof vi.fn> };

/** 六维全可用的画像（个股常见形态）。 */
const FULL = {
  code: "600519.SH",
  name: "贵州茅台",
  ts: "2026-09-21T21:00:00",
  availability: {
    snapshot: "ok",
    profile: "ok",
    capital: "ok",
    performance: "ok",
    moneyflow: "ok",
    valuation: "ok",
  },
  snapshot: {
    last: 1252.57, pre_close: 1266.98, change: -14.41, change_pct: -1.14,
    open: 1262.99, high: 1265.88, low: 1256.1, volume: 16571, amount: 2116621850,
  },
  profile: {
    name: "贵州茅台", exchange: "上海证券交易所", board: "主板",
    high_limit: 1393.68, low_limit: 1140.28, industry: "酿酒行业", concepts: ["白酒"],
  },
  capital: {
    total_shares: 1256197800, circulating_shares: 1256197800,
    turnover_rate: 0.2, total_mktcap: 1.573e12, float_mktcap: 1.573e12,
  },
  performance: {
    chg_5d: 1.2, chg_20d: -0.8, chg_60d: 3.4,
    high_52w: 1800.0, low_52w: 1200.0, pct_in_52w: 12.5, as_of: "20260921",
  },
  moneyflow: { 主力净流入: 12345678 },
  valuation: {
    report_time: "2025-03-31", eps: 2.0, bps: 100.0, roe: 6.0,
    pe: 626.29, pb: 12.53, metrics_source: "broker",
  },
};

beforeEach(() => {
  api.analysis.mockReset();
});

afterEach(cleanup);

describe("FundamentalsPanel · 6 维一排 + 默认收起", () => {
  it("默认收起：6 维标题都在，但明细单元格一个都不渲染", async () => {
    api.analysis.mockResolvedValue(FULL);
    const { container } = render(<FundamentalsPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("维度可用 6 / 6"));
    const txt = container.textContent || "";

    // ① 六个维度标题都在（一排看得见）
    for (const title of ["行情快照", "估值", "近期表现", "股本 / 市值", "标的档案", "资金流"]) {
      expect(txt, `缺少维度标题 ${title}`).toContain(title);
    }
    // ② 可用性角标都在（否则用户不知道哪一维是空的）
    expect((txt.match(/正常/g) || []).length).toBeGreaterThanOrEqual(6);

    // ③ ★ 明细**不得渲染**（这些标签只出现在展开区里）
    for (const detailOnly of [
      "市盈率 PE", "每股净资产", "净资产收益率",
      "52 周高", "52 周低", "52 周分位",
      "总股本", "流通股本", "报告期",
    ]) {
      expect(txt, `收起时不该出现明细 ${detailOnly}`).not.toContain(detailOnly);
    }
    // ④ 收起态要给出「点击展开」的引导，而不是让用户自己猜
    expect(txt).toContain("点击任一维度查看明细");
  });

  it("收起态的摘要是真数据（不是一排破折号）", async () => {
    api.analysis.mockResolvedValue(FULL);
    const { container } = render(<FundamentalsPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("维度可用 6 / 6"));
    const txt = container.textContent || "";

    // 每维都要有能替代「点开看一眼」的关键数值
    expect(txt).toContain("最新 1252.57");        // 行情快照
    expect(txt).toContain("PE 626.29");           // 估值
    expect(txt).toContain("PB 12.53");
    expect(txt).toContain("5日");                 // 近期表现
    expect(txt).toContain("贵州茅台 · 主板");      // 标的档案
    expect(txt).toContain("主力净流入");           // 资金流
  });

  it("点击某一维 → 只展开该维明细；再点 → 收起", async () => {
    api.analysis.mockResolvedValue(FULL);
    const { container, getByTitle } = render(<FundamentalsPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("维度可用 6 / 6"));

    fireEvent.click(getByTitle("展开「估值」明细"));
    await waitFor(() => expect(container.textContent).toContain("市盈率 PE"));
    expect(container.textContent).toContain("报告期");
    expect(container.textContent).toContain("2025-03-31");
    // 其它维仍收起（一次只展开一维：坞高是从 K 线借的）
    expect(container.textContent).not.toContain("52 周高");
    expect(container.textContent).not.toContain("总股本");

    // 再点收起
    fireEvent.click(getByTitle("收起「估值」明细"));
    await waitFor(() => expect(container.textContent).not.toContain("市盈率 PE"));
  });

  it("展开另一维会替换前一维（不会同时铺开两块）", async () => {
    api.analysis.mockResolvedValue(FULL);
    const { container, getByTitle } = render(<FundamentalsPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("维度可用 6 / 6"));

    fireEvent.click(getByTitle("展开「近期表现」明细"));
    await waitFor(() => expect(container.textContent).toContain("52 周高"));

    fireEvent.click(getByTitle("展开「股本 / 市值」明细"));
    await waitFor(() => expect(container.textContent).toContain("总股本"));
    expect(container.textContent).not.toContain("52 周高");
  });

  it("不可用的维度：卡片上给短原因，展开后给可操作说明", async () => {
    // 实测形态：指数（或终端没下财务数据）⇒ valuation/performance 不可用
    api.analysis.mockResolvedValue({
      ...FULL,
      availability: { ...FULL.availability, valuation: "unavailable", performance: "unavailable" },
      valuation: { report_time: null, eps: null, bps: null, roe: null, pe: null, pb: null },
      performance: null,
    });
    const { container, getByTitle } = render(<FundamentalsPanel code="000001.SH" />);
    await waitFor(() => expect(container.textContent).toContain("维度可用 4 / 6"));
    const txt = container.textContent || "";

    // 收起态：说**原因**，而不是一排破折号（破折号不携带任何信息）
    expect(txt).toContain("券商无财务数据");
    expect(txt).toContain("本地日线不足");
    expect(txt).toContain("无数据");

    // 展开后：给「怎么解决」
    fireEvent.click(getByTitle("展开「估值」明细"));
    await waitFor(() => expect(container.textContent).toContain("估值需券商财务接口"));
  });

  it("估值来源可追溯（券商现算 vs 行情源直供口径不同）", async () => {
    api.analysis.mockResolvedValue({
      ...FULL,
      valuation: { ...FULL.valuation, metrics_source: "tencent" },
    });
    const { container, getByTitle } = render(<FundamentalsPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("维度可用 6 / 6"));
    fireEvent.click(getByTitle("展开「估值」明细"));
    await waitFor(() => expect(container.textContent).toContain("估值来源：腾讯行情"));
  });
});
