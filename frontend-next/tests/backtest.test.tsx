import { describe, expect, it, vi, beforeEach, beforeAll, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { BacktestJob } from "@/services/api";

/**
 * 「策略回测」页的契约回归。
 *
 * 这个页面是为**「后端能力已存在但界面不可达」**补的（`/backtest/jobs` 全套接口
 * 早就有了，`src/` 里零调用、`routes.tsx` 里无页面）。所以这里锁的不是布局，
 * 而是几条「错了会让用户误判结果」的区分：
 *
 * ① **作业有两种形状**：内存作业给 `params`/`result` 对象，DB 行给
 *    `params_json`/`result_json` 字符串。只吃一种 ⇒ 列表里一半作业显示
 *    「没有结果」而实际早就跑完了 —— 这会被读成「回测失败了」；
 * ② **DELETE 是「取消」不是「删除」**，且只对排队/运行中的作业有效，
 *    对已结束的作业返回 `cancelled:false` ⇒ 一律提示「已取消」就是假成功；
 * ③ **指标里的诚信标注必须照显示**：样本不足时后端主动声明 VaR/CVaR 未输出，
 *    界面若吞掉这句话，用户会把「没有这个字段」读成「没有尾部风险」。
 */
// ⚠️ jsdom 没有 canvas，ECharts 一初始化就 `clearRect of null` ⇒ 桩掉图表。
// 这里测的是**契约与文案**，不是曲线画得对不对（画面对不对归浏览器实测）。
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));

vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    backtestApi: {
      jobs: vi.fn(),
      job: vi.fn(),
      submit: vi.fn(),
      cancel: vi.fn(),
      batchDelete: vi.fn(),
      sweep: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import { Backtest } from "@/domains/research/Backtest";
// eslint-disable-next-line import/first
import { backtestApi } from "@/services/api";
// eslint-disable-next-line import/first
import { useBrokerStore } from "@/stores/broker";

const api = backtestApi as unknown as {
  jobs: ReturnType<typeof vi.fn>;
  job: ReturnType<typeof vi.fn>;
  submit: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
  batchDelete: ReturnType<typeof vi.fn>;
  sweep: ReturnType<typeof vi.fn>;
};

/**
 * ⚠️ `DataTable` 走 `@tanstack/react-virtual`，**jsdom 里元素高度恒为 0 ⇒ 一行都不渲染**，
 * 断言行内文案会报成误导性的 "text is broken up by multiple elements"。补高度桩即可。
 */
beforeAll(() => {
  for (const k of ["offsetHeight", "clientHeight"]) {
    Object.defineProperty(HTMLElement.prototype, k, { configurable: true, value: 600 });
  }
  Element.prototype.getBoundingClientRect = () =>
    ({ width: 1200, height: 600, top: 0, left: 0, bottom: 600, right: 1200,
       x: 0, y: 0, toJSON() {} }) as DOMRect;
});

/** 内存作业形状（result 是对象）。 */
const memJob: BacktestJob = {
  id: "aaaaaaaaaaaa", kind: "backtest", status: "done", progress: 100,
  params: { symbol: "600519.SH" },
  result: { metrics: { total_return: 0.1234, sharpe: 1.8, max_drawdown: -0.08,
                       win_rate: 0.6, trade_count: 12, rating: "A" },
            equity: [100000, 101000, 112340], trades: [{ side: "buy", pnl: 0 }] },
  created_at: "2026-09-19T10:00:00",
};

/** DB 行形状（result_json / params_json 是字符串）。 */
const dbJob: BacktestJob = {
  id: "bbbbbbbbbbbb", kind: "backtest", status: "done", progress: 100,
  params_json: JSON.stringify({ symbol: "000001.SZ" }),
  result_json: JSON.stringify({
    metrics: { total_return: -0.05, sharpe: 0.2, max_drawdown: -0.2,
               win_rate: 0.3, trade_count: 4, rating: "C",
               tail_metrics_note: "样本不足（12 个收益观测 < 20），VaR/CVaR 不具统计意义，未输出" },
    equity: [100000, 95000],
  }),
  created_at: "2026-09-19T09:00:00",
};

/** fireEvent.click 需要确定的元素（noUncheckedIndexedAccess 下下标可能 undefined）。 */
async function clickText(label: string, nth = 0) {
  const { fireEvent } = await import("@testing-library/react");
  const el = screen.getAllByText(label)[nth];
  if (!el) throw new Error(`未找到可点击元素：${label} #${nth}`);
  fireEvent.click(el);
}

beforeEach(() => {
  api.jobs.mockResolvedValue([memJob, dbJob]);
  // 券商连接态是全局 store ⇒ 每个用例都必须显式给定，否则会串味
  useBrokerStore.setState({ connections: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useBrokerStore.setState({ connections: [] });
});

describe("策略回测页", () => {
  it("两种作业形状都能渲染出标的（不能只吃内存作业）", async () => {
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText("600519.SH")).toBeTruthy());
    // ★ DB 行那条的 params 藏在 params_json 里 —— 解析漏了就显示「—」
    expect(screen.getByText("000001.SZ")).toBeTruthy();
  });

  it("完成态：显示关键指标", async () => {
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText("600519.SH")).toBeTruthy());
    await clickText("结果", 0);
    await waitFor(() => expect(screen.getByText("总收益")).toBeTruthy());
    // 0.1234 → 12.34%
    expect(screen.getByText("12.34%")).toBeTruthy();
    expect(screen.getByText("1.800")).toBeTruthy();
  });

  it("诚信标注必须照显示（样本不足时后端明确说 VaR/CVaR 未输出）", async () => {
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText("000001.SZ")).toBeTruthy());
    await clickText("结果", 1);
    await waitFor(() => expect(screen.getByText(/VaR\/CVaR 不具统计意义/)).toBeTruthy());
  });

  it("取消已结束的作业：不能谎报「已取消」", async () => {
    api.cancel.mockResolvedValue({ cancelled: false });
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText("600519.SH")).toBeTruthy());
    // ConfirmButton 是两段式：先点一次进入待确认态（默认文案「确认删除」），再点才执行
    await clickText("取消", 0);
    await waitFor(() => expect(screen.getByText("确认删除")).toBeTruthy());
    await clickText("确认删除", 0);
    await waitFor(() => expect(screen.getByText(/无法取消/)).toBeTruthy());
    expect(screen.queryByText(/已取消 #/)).toBeNull();
  });

  it("接口不可用时给出失败原因，而不是渲染成「还没有回测作业」", async () => {
    api.jobs.mockRejectedValue(new Error("后端未启动"));
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText(/回测作业加载失败：后端未启动/)).toBeTruthy());
  });

  /**
   * ★ 2026-09-22 修的真缺陷的护栏：空列表**不能**把成因说成「需先连接券商」。
   *
   * 实测现场（打包态 0.3.0）：状态栏显示「券商 1/1 · 迅投XTQuant(bridge)」已连接，
   * 回测页的空态却写着「还没有回测作业。回测基于券商真实历史 K 线，需先连接券商。」
   * —— 列表接口读的是本地作业表，与券商无关；空列表只有一个成因：还没提交过。
   * 把「没提交」说成「没连券商」，用户会去连接管理页白跑一趟。
   */
  it("空列表 · 已连券商：不得再说「需先连接券商」（错误归因）", async () => {
    api.jobs.mockResolvedValue([]);
    useBrokerStore.setState({ connections: [{ connected: true } as never] });
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText(/还没有回测作业/)).toBeTruthy());
    expect(screen.queryByText(/需先连接券商/)).toBeNull();
    expect(screen.queryByText(/当前未连接券商/)).toBeNull();
    // 连上了就不该再挂「未连接券商…」的常驻告示（与 OrderForm 同类缺陷）
    expect(screen.queryByText(/未连接券商：回测依赖/)).toBeNull();
  });

  it("空列表 · 未连券商：如实说明成因并给出路（不能只报问题不给处置）", async () => {
    api.jobs.mockResolvedValue([]);
    useBrokerStore.setState({ connections: [] });
    render(<Backtest />);
    await waitFor(() => expect(screen.getByText(/当前未连接券商/)).toBeTruthy());
    expect(screen.getByText("去连接")).toBeTruthy();
  });
});
