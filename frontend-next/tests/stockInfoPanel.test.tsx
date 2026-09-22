import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";

/**
 * 「基本信息」面板的**渲染**测试 —— 2026-09-21 扩展字段。
 *
 * ## 为什么这里能断言渲染
 * 这个面板走的是普通 `kvGrid` 网格（**不是** DataTable 虚拟表格），jsdom 下
 * 照样渲染出来 —— 所以不需要退到源码扫描，可以真正断言「屏幕上显示什么」。
 *
 * ## 最要紧的一条
 * 后端拿不到某个字段时给的是 **`null`**（不是 0）。界面必须显示 `--`：
 * 「总市值 0」「市盈率 0.00」会被读成真实数据 —— **假数据比没数据更危险**。
 * 第 2 个用例就是这条的锁。
 */
vi.mock("@/services/api/market", async () => {
  const actual = await vi.importActual<typeof import("@/services/api/market")>(
    "@/services/api/market",
  );
  return {
    ...actual,
    marketApi: { ...actual.marketApi, stockInfo: vi.fn() },
  };
});

// eslint-disable-next-line import/first
import { marketApi } from "@/services/api/market";
// eslint-disable-next-line import/first
import { StockInfoPanel } from "@/domains/market/panels/StockInfoPanel";

const api = marketApi as unknown as { stockInfo: ReturnType<typeof vi.fn> };

const FULL = {
  code: "600519.SH",
  name: "贵州茅台",
  exchange: "上海证券交易所",
  board: "主板",
  high_limit: 1393.68,
  low_limit: 1140.28,
  pre_close: 1266.98,
  industry: "酿酒行业",
  concepts: ["白酒", "MSCI"],
  source: "tencent",
  open: 1262.99,
  high: 1265.88,
  low: 1256.10,
  avg_price: 1259.84,
  amplitude: 0.77,
  turnover_rate: 0.2,
  volume_ratio: 1.14,
  pe_ttm: 19.3,
  pb: 6.25,
  circ_mv: 15715.03 * 1e8,
  total_mv: 15715.03 * 1e8,
  amount: 313585 * 1e4,
};

beforeEach(() => {
  api.stockInfo.mockReset();
});

afterEach(cleanup);

describe("StockInfoPanel · 扩展字段", () => {
  it("市值 / 估值 / 换手 等扩展字段都渲染出来", async () => {
    api.stockInfo.mockResolvedValue(FULL);
    const { container } = render(<StockInfoPanel code="600519.SH" />);
    await waitFor(() => expect(api.stockInfo).toHaveBeenCalled());
    const txt = () => container.textContent || "";
    await waitFor(() => expect(txt()).toContain("贵州茅台"));

    for (const must of [
      "总市值", "15715.03亿",      // fmtAmount：元 → 亿
      "流通市值",
      "市盈(TTM)", "19.30",
      "市净率", "6.25",
      "换手率", "0.20%",           // 无符号百分比（不是 +0.20%）
      "振幅", "0.77%",
      "量比", "1.14",
      "均价", "1259.84",
      "涨停价", "1393.68",
      "跌停价", "1140.28",
      "今开", "1262.99",
    ]) {
      expect(txt(), `缺少 ${must}`).toContain(must);
    }
    // 换手率/振幅是**比率**不是涨跌 ⇒ 不许带正负号（fmtPct 才带）
    expect(txt()).not.toContain("+0.20%");
    // 概念标签
    expect(txt()).toContain("白酒");
    expect(txt()).toContain("MSCI");
  });

  it("后端给 null 的字段显示 --，绝不能显示 0", async () => {
    api.stockInfo.mockResolvedValue({
      code: "000001.SZ",
      name: "平安银行",
      exchange: "深交所",
      board: "主板",
      high_limit: null,
      low_limit: null,
      pre_close: 11.66,
      industry: "",
      concepts: [],
      source: "eltdx",
      open: null, high: null, low: null, avg_price: null, amplitude: null,
      turnover_rate: null, volume_ratio: null, pe_ttm: null, pb: null,
      circ_mv: null, total_mv: null, amount: null,
    });
    const { container } = render(<StockInfoPanel code="000001.SZ" />);
    await waitFor(() => expect(container.textContent).toContain("平安银行"));
    const txt = container.textContent || "";

    // 该有的键位还在（否则用户以为面板坏了）
    for (const key of ["总市值", "流通市值", "市盈(TTM)", "市净率", "换手率", "振幅", "量比"]) {
      expect(txt, `缺字段行 ${key}`).toContain(key);
    }
    // ★ 关键：值必须是 "--"，**不允许**出现 "0.00" / "0亿"
    const dashes = (txt.match(/--/g) || []).length;
    expect(dashes).toBeGreaterThanOrEqual(7);
    expect(txt).not.toContain("0.00");
    expect(txt).not.toContain("0亿");
  });

  it("行情派生字段与详情源不同源时，两个源都要标出来", async () => {
    // 打包版实测形态：详情源是 eltdx（不提供市值/PE），字段由公开源补齐。
    // 只显示 eltdx 会被当成「市值也是 eltdx 的数据」⇒ 必须同时标出补来源。
    api.stockInfo.mockResolvedValue({
      ...FULL,
      source: "eltdx",
      metrics_source: "tencent",
    });
    const { container } = render(<StockInfoPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("贵州茅台"));
    const txt = container.textContent || "";
    expect(txt).toContain("eltdx");
    expect(txt).toContain("行情 · tencent");
  });

  it("同源时不重复标（避免噪音）", async () => {
    api.stockInfo.mockResolvedValue({ ...FULL, source: "tencent", metrics_source: "tencent" });
    const { container } = render(<StockInfoPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("贵州茅台"));
    expect(container.textContent || "").not.toContain("行情 · tencent");
  });

  it("降级 note 必须显示（否则推断值会被当成事实）", async () => {
    api.stockInfo.mockResolvedValue({
      code: "600519.SH",
      name: "",
      board: "主板",
      note: "未连接券商且 TDX 行情源不可用，板块按代码前缀推断",
      high_limit: null, low_limit: null, pre_close: null,
      industry: "", concepts: [],
    });
    const { container } = render(<StockInfoPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("按代码前缀推断"));
  });
});
