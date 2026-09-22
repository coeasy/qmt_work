import { describe, expect, it, vi, beforeEach, beforeAll, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { MarketCatalogItem, MarketStrategy } from "@/services/api";

/**
 * 「策略市场」页的契约回归（同样是补「后端已有、界面没有」的缺口）。
 *
 * 锁的三条：**错了会让用户以为操作成功了**的区分：
 *
 * ① `install` 需要 `client_path`，空值后端直接 400 ⇒ 前端必须**先拦下并说清缺什么**，
 *    静默发一个必失败的请求，是最典型的「点了没反应」；
 * ② 导入/导出收的是**服务端可见路径**，文件不存在时后端报「bundle 不存在：<路径>」
 *    ⇒ 原因要原样显示，不能吞成「导入失败」；
 * ③ `content` 是策略正文，为空时后端会存进一条空策略 ⇒ 前端先拦，避免市场里
 *    出现「能安装但跑不起来」的条目。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    strategyMarketApi: {
      catalog: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      publish: vi.fn(),
      install: vi.fn(),
      exportBundle: vi.fn(),
      importBundle: vi.fn(),
      exportJson: vi.fn(),
      importJson: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import { StrategyMarket } from "@/domains/research/StrategyMarket";
// eslint-disable-next-line import/first
import { strategyMarketApi } from "@/services/api";

const api = strategyMarketApi as unknown as {
  catalog: ReturnType<typeof vi.fn>;
  list: ReturnType<typeof vi.fn>;
  get: ReturnType<typeof vi.fn>;
  publish: ReturnType<typeof vi.fn>;
  install: ReturnType<typeof vi.fn>;
  exportBundle: ReturnType<typeof vi.fn>;
  importBundle: ReturnType<typeof vi.fn>;
  exportJson: ReturnType<typeof vi.fn>;
  importJson: ReturnType<typeof vi.fn>;
};

/** DataTable 走虚拟滚动，jsdom 高度恒为 0 ⇒ 不补桩一行都不渲染。 */
beforeAll(() => {
  for (const k of ["offsetHeight", "clientHeight"]) {
    Object.defineProperty(HTMLElement.prototype, k, { configurable: true, value: 600 });
  }
  Element.prototype.getBoundingClientRect = () =>
    ({ width: 1200, height: 600, top: 0, left: 0, bottom: 600, right: 1200,
       x: 0, y: 0, toJSON() {} }) as DOMRect;
});

const catalog: MarketCatalogItem[] = [
  { id: "ma_cross", name: "双均线", type: "ma_cross", description: "金叉买死叉卖",
    params_schema: [{ name: "fast" }, { name: "slow" }] },
];
const market: MarketStrategy[] = [
  { id: "usr_001", title: "我的均线", author: "me", type: "custom",
    tags: ["均线", "日线"], downloads: 3, created_at: "2026-09-19T10:00:00" },
];

beforeEach(() => {
  api.catalog.mockResolvedValue(catalog);
  api.list.mockResolvedValue(market);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("策略市场页", () => {
  it("模板目录与市场策略都渲染", async () => {
    render(<StrategyMarket />);
    await waitFor(() => expect(screen.getByText("双均线")).toBeTruthy());
    expect(screen.getByText("我的均线")).toBeTruthy();
    // 后端把 tags_json 解成数组，前端直接渲染
    expect(screen.getByText("均线 / 日线")).toBeTruthy();
  });

  it("没填客户端目录就点安装：就地拦下并说清缺什么，不静默发必失败的请求", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<StrategyMarket />);
    await waitFor(() => expect(screen.getByText("我的均线")).toBeTruthy());
    const btn = screen.getAllByText("安装")[0];
    if (!btn) throw new Error("未找到安装按钮");
    fireEvent.click(btn);
    await waitFor(() => expect(screen.getByText(/请先填写 QMT 客户端安装目录/)).toBeTruthy());
    expect(api.install).not.toHaveBeenCalled();
  });

  it("导入路径不存在时把后端原因原样显示（不能吞成「导入失败」）", async () => {
    api.importBundle.mockRejectedValue(new Error("bundle 不存在：D:/nope.zip"));
    const { fireEvent } = await import("@testing-library/react");
    render(<StrategyMarket />);
    await waitFor(() => expect(screen.getByText("我的均线")).toBeTruthy());

    const path = screen.getByPlaceholderText(/服务端可见的/);
    fireEvent.change(path, { target: { value: "D:/nope.zip" } });
    fireEvent.click(screen.getByText("导入 bundle"));
    await waitFor(() => expect(screen.getByText(/bundle 不存在：D:\/nope\.zip/)).toBeTruthy());
  });

  it("正文为空时不允许发布（避免市场里出现跑不起来的空策略）", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<StrategyMarket />);
    await waitFor(() => expect(screen.getByText("我的均线")).toBeTruthy());
    fireEvent.click(screen.getByText("发布"));
    await waitFor(() => expect(screen.getByText("策略正文不能为空")).toBeTruthy());
    expect(api.publish).not.toHaveBeenCalled();
  });

  it("接口不可用时给出失败原因，而不是渲染成「市场里还没有策略」", async () => {
    api.list.mockRejectedValue(new Error("后端未启动"));
    render(<StrategyMarket />);
    await waitFor(() => expect(screen.getByText(/市场策略加载失败：后端未启动/)).toBeTruthy());
  });
});
