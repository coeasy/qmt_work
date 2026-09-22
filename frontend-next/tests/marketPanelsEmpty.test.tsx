import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";

/**
 * 行情面板**空态的成因分叉** —— 2026-09-22 第 23 轮。
 *
 * ## 为什么单独锁这一条
 * 本项目的空态缺陷有一个反复出现的形态：**把「前置条件」说成「空态的成因」**，
 * 或者**用「或」把两个完全不同的成因糊在一起**。用户读到的结果是「不知道去修哪一边」，
 * 甚至被送去做一件完全无关的事（去「连接管理」白跑一趟）。
 *
 * 已修过的同类：`Backtest.tsx`（空态硬写「需先连接券商」，而该列表读本地作业表）、
 * `Dashboard.tsx`（「当前无持仓（或账户未连接）」）、以及本文件覆盖的这两个面板。
 *
 * ## 两条判据都是**可证伪**的
 * 1. 通道状态是**可判的**（`useQuotesStore.socketState`）⇒ 两个成因必须分开说；
 *    退回「WS 未连接或该标的不在推送范围」这种糊在一起的写法，第 1 条必红。
 * 2. 通道**开着**却没行情时**不得**再给「去连接」—— 那是假出口；把出口加回去，第 2 条必红。
 * 3. `L2Panel` 能走到空态 ⇒ `/market/l2` 返回了 200（未连券商时 `no_broker()` 返 503，
 *    已被 error 分支接管）⇒ 空态里说「未连接券商时为空是预期行为」**必然是错的**；
 *    把这句话加回去，第 4 条必红。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    marketApi: { ...actual.marketApi, l2: vi.fn() },
  };
});

// eslint-disable-next-line import/first
import { marketApi } from "@/services/api";
// eslint-disable-next-line import/first
import { useQuotesStore } from "@/stores/quotes";
// eslint-disable-next-line import/first
import { OrderBookPanel } from "@/domains/market/panels/OrderBookPanel";
// eslint-disable-next-line import/first
import { L2Panel } from "@/domains/market/panels/L2Panel";

const l2 = (marketApi as unknown as { l2: ReturnType<typeof vi.fn> }).l2;

beforeEach(() => {
  l2.mockReset();
  useQuotesStore.setState({ quotes: {}, refs: {}, socketState: "idle", lastSeq: 0 });
});

afterEach(cleanup);

describe("OrderBookPanel · 空态按行情通道的真实状态分叉", () => {
  it("通道未连接：说明通道状态并给出路，不得用「或」把两个成因糊在一起", () => {
    useQuotesStore.setState({ socketState: "closed" });
    const { container } = render(<OrderBookPanel code="600519.SH" />);
    const txt = container.textContent || "";

    expect(txt).toContain("行情通道已断开");
    expect(txt).toContain("去连接");
    // ★ 旧的错误形态：一个括号把「WS 未连接」与「不在推送范围」糊在一起
    expect(txt).not.toContain("WS 未连接或该标的不在推送范围");
  });

  it("通道 connecting 时也要如实说是「正在连接」，不能一律说「未连接」", () => {
    useQuotesStore.setState({ socketState: "connecting" });
    const { container } = render(<OrderBookPanel code="600519.SH" />);
    expect(container.textContent || "").toContain("行情通道正在连接");
  });

  it("通道已连接但无推送：不得再给「去连接」假出口（通道明明是好的）", () => {
    useQuotesStore.setState({ socketState: "open" });
    const { container } = render(<OrderBookPanel code="600519.SH" />);
    const txt = container.textContent || "";

    expect(txt).toContain("行情通道已连接，但尚未收到该标的的推送");
    // ★ 假出口：通道是好的，把用户送去「连接管理」只会白跑
    expect(txt).not.toContain("去连接");
    expect(txt).not.toContain("行情通道未连接");
  });
});

describe("L2Panel · 空态不得把「已连接」说成「未连接券商」", () => {
  it("端点报错（未连券商 → 503）：错误分支必须说清 503 是契约预期", async () => {
    l2.mockRejectedValue(new Error("未连接券商，该端点不可用"));
    const { container } = render(<L2Panel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("未连接券商，该端点不可用"));

    const txt = container.textContent || "";
    expect(txt).toContain("未连接券商时该端点返回 503");
  });

  it("端点正常返回但为空：不得再说「未连接券商时为空是预期行为」（该分支券商必然已连接）", async () => {
    l2.mockResolvedValue([]);
    const { container } = render(<L2Panel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("券商已连接"));

    const txt = container.textContent || "";
    expect(txt).not.toContain("未连接券商时为空是预期行为");
    // 这个分支没有「去连接」可做的事（券商是连着的），不得给假出口
    expect(txt).not.toContain("去连接");
  });
});
