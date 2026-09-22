import { describe, expect, it, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";

/**
 * 分仓再平衡页回归测试。锁住三件事：
 *
 *  ① **危险操作护栏**：「仅生成计划」直接执行；选「真实下单」必须先弹确认，
 *     确认后才真正调用接口 —— 真实下单走 SignalRouter 且**不可撤销**，
 *     点一下就下单是不可接受的。
 *  ② **跳过行不得渲染成买卖**：涨跌停跳过的行只有 ``{code, skipped:"limit", diff}``，
 *     **没有 direction**。若按买入/卖出渲染，等于把「没调」显示成「调了」，
 *     是会让用户做错决策的静默错误。
 *  ③ 「没生成」与「生成了但无需调仓」是两件事，文案必须区分。
 *
 * ★ 表格是虚拟滚动的，jsdom 下容器高度 0 ⇒ 行不渲染，故行内容改由
 * ``orderCols`` 的 render 直接单测（与 resultCols 同一套路）。
 */

const BUY: OrderRow = { code: "600519.SH", direction: "buy", volume: 100, price: 1800.5, diff: 180050 };
const SELL: OrderRow = { code: "000001.SZ", direction: "sell", volume: 200, price: 11.2, diff: -2240 };
// ★ 跳过行：只有 code/skipped/diff，**没有 direction**
const SKIPPED: OrderRow = { code: "300750.SZ", skipped: "limit", diff: 5000 };
const ORDERS: OrderRow[] = [BUY, SELL, SKIPPED];

vi.mock("@/services/api", () => ({
  portfolioApi: { rebalance: vi.fn() },
}));

// ConnSelect 依赖 broker store 与轮询，单测里替身成普通 select
vi.mock("@/domains/research/ConnSelect", () => ({
  ConnSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <select aria-label="账户连接" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">自动</option>
      <option value="c1">连接1</option>
    </select>
  ),
}));

import { portfolioApi } from "@/services/api";
import { Rebalance, orderCols, type OrderRow } from "@/domains/trading/Rebalance";

const mock = portfolioApi.rebalance as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Rebalance · 分仓再平衡", () => {
  it("① 仅生成计划：直接执行，不弹确认", async () => {
    mock.mockResolvedValue({ orders: ORDERS, generated: 2 });
    render(<Rebalance />);

    fireEvent.click(screen.getByText("生成调仓计划"));
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));

    // 不弹确认框，且 do_trade 传 false
    expect(screen.queryByText("确认真实下单")).toBeNull();
    expect(mock.mock.calls[0]?.[0]?.do_trade).toBe(false);
    await waitFor(() => expect(screen.getByText(/生成 2 条调仓单/)).toBeTruthy());
    expect(screen.getByText(/1 条因涨跌停跳过/)).toBeTruthy();
  });

  it("② 真实下单：先确认，取消则不调用接口", async () => {
    render(<Rebalance />);
    fireEvent.change(screen.getByLabelText("是否下单"), { target: { value: "1" } });

    fireEvent.click(screen.getByText("生成调仓计划"));
    await waitFor(() => expect(screen.getByText("确认真实下单")).toBeTruthy());
    expect(mock).not.toHaveBeenCalled();   // 关键：未确认前绝不下单

    fireEvent.click(screen.getByText("取消"));
    await waitFor(() => expect(screen.queryByText("确认真实下单")).toBeNull());
    expect(mock).not.toHaveBeenCalled();
  });

  it("③ 确认后才下单，且 do_trade 传 true", async () => {
    mock.mockResolvedValue({ orders: ORDERS, generated: 2 });
    render(<Rebalance />);
    fireEvent.change(screen.getByLabelText("是否下单"), { target: { value: "1" } });
    fireEvent.click(screen.getByText("生成调仓计划"));
    await waitFor(() => expect(screen.getByText("确认真实下单")).toBeTruthy());

    fireEvent.click(screen.getByText("确认下单"));
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));
    expect(mock.mock.calls[0]?.[0]?.do_trade).toBe(true);
  });

  it("④ 跳过行渲染为「跳过」，不冒充买卖", () => {
    const dirCol = orderCols.find((c) => c.key === "dir")!;
    const noteCol = orderCols.find((c) => c.key === "note")!;

    const skipped = dirCol.render(SKIPPED, 2) as React.ReactElement;
    expect(skipped.props.children).toBe("跳过");

    // 反例：正常买入/卖出仍要正确渲染
    expect((dirCol.render(BUY, 0) as React.ReactElement).props.children).toBe("买入");
    expect((dirCol.render(SELL, 1) as React.ReactElement).props.children).toBe("卖出");

    expect((noteCol.render(SKIPPED, 2) as React.ReactElement).props.children)
      .toBe("触及涨跌停，本次不调");
  });

  it("⑤ 缺字段显示占位符，不用 0 冒充", () => {
    const volCol = orderCols.find((c) => c.key === "vol")!;
    const diffCol = orderCols.find((c) => c.key === "diff")!;
    // 跳过行没有 volume
    expect(volCol.render(SKIPPED, 2)).toBe("--");
    expect(volCol.render(BUY, 0)).toBe("100");
    expect(diffCol.render({ code: "x" }, 0)).toBe("--");
  });

  it("⑥ 「未生成」与「无需调仓」文案不同", async () => {
    mock.mockResolvedValue({ orders: [], generated: 0 });
    render(<Rebalance />);
    // 空态文案带了「下一步」引导（见 Rebalance.tsx），这里用前缀匹配，
    // 免得每次改文案都要动断言 —— 真正要守的是「未生成 ≠ 无需调仓」这个区分。
    expect(screen.getByText(/^尚未生成调仓计划/)).toBeTruthy();

    fireEvent.click(screen.getByText("生成调仓计划"));
    await waitFor(() => expect(screen.getByText("无需调仓（所有标的差额均低于阈值）")).toBeTruthy());
    expect(screen.queryByText(/^尚未生成调仓计划/)).toBeNull();
  });
});
