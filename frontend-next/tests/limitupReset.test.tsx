import { describe, expect, it, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";

/**
 * 涨停监控「重置触发」护栏。
 *
 * ★ 这条不变量比普通的两段式确认更硬，因为它解掉的是**一层防重复下单的保险**：
 *   后端 `engines/limitup.py::reset_triggered` 清的是 `_triggered` ——
 *   `_maybe_trigger` 里 `if code in self._triggered: return` 的去重集合。
 *   清空 ⇒ 池中已触发过的标的**再次触发** ⇒ do_trade=true 时**重复真实下单**。
 *
 * 所以它不是一个「把计数器归零」的无害按钮。误点一次 = 多一批委托。
 * 这里把「一次点击绝不生效」与「确认框必须说清后果」两件事钉死。
 */

vi.mock("@/services/api", () => ({
  limitupApi: {
    status: vi.fn(),
    addPool: vi.fn(),
    removePool: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
    reset: vi.fn(),
  },
  marketApi: { limitupScan: vi.fn() },
}));

// 实时行情订阅依赖 WS / store，单测里替身成空表（本用例只关心重置护栏）
vi.mock("@/hooks/useLiveQuotes", () => ({
  useLiveQuotes: () => ({}),
}));

import { limitupApi } from "@/services/api";
import { LimitUp } from "@/domains/trading/LimitUp";

const statusMock = limitupApi.status as unknown as ReturnType<typeof vi.fn>;
const resetMock = limitupApi.reset as unknown as ReturnType<typeof vi.fn>;

/** do_trade=true + 已触发过 3 只 —— 最危险的组合，确认框必须按这个口径提示 */
const STATUS = {
  running: true,
  interval: 2,
  limit_pct: 0.1,
  cutoff: "10:00",
  min_rise: 0.03,
  buy_volume: 100,
  do_trade: true,
  pool: [{ code: "600519.SH", name: "贵州茅台" }],
  total_triggered: 3,
  events: [],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("LimitUp · 重置触发必须确认", () => {
  it("① 点一次不生效：先弹确认，且绝不调用 reset", async () => {
    statusMock.mockResolvedValue(STATUS);
    render(<LimitUp />);
    await waitFor(() => expect(screen.getByText("重置触发")).toBeTruthy());

    fireEvent.click(screen.getByText("重置触发"));

    expect(resetMock).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("重置涨停触发记录")).toBeTruthy());
  });

  it("② 确认后才真正 reset", async () => {
    statusMock.mockResolvedValue(STATUS);
    resetMock.mockResolvedValue({ ok: true });
    render(<LimitUp />);
    await waitFor(() => expect(screen.getByText("重置触发")).toBeTruthy());

    fireEvent.click(screen.getByText("重置触发"));
    await waitFor(() => expect(screen.getByText("重置涨停触发记录")).toBeTruthy());
    fireEvent.click(screen.getByText("确认重置"));

    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(1));
  });

  it("③ 确认框必须说清「再次触发」与「重复下单」", async () => {
    statusMock.mockResolvedValue(STATUS);
    render(<LimitUp />);
    await waitFor(() => expect(screen.getByText("重置触发")).toBeTruthy());

    fireEvent.click(screen.getByText("重置触发"));
    await waitFor(() => expect(screen.getByText("重置涨停触发记录")).toBeTruthy());

    // 只写「确定要重置吗？」是不够的：用户不知道这会解除去重保险。
    // 用 getAll：正则会同时命中外层段落与里面的 <b>，getBy 会因「多个匹配」报错。
    expect(screen.getAllByText(/再次触发/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/重复下单/).length).toBeGreaterThan(0);
  });
});
