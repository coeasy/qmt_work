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
/**
 * 3. `L2Panel` 能走到空态 ⇒ `/market/l2` 返回了 200（未连券商时 `no_broker()` 返 503，
 *    已被 error 分支接管）⇒ 空态里说「未连接券商时为空是预期行为」**必然是错的**；
 *    把这句话加回去，第 4 条必红。
 *
 * ★ 2026-10-03 第 3 条判据（本轮修的断链，必须用测试钉死）：
 *   `OrderBookPanel` **不得只认 WS 推送**。WS 行情推送的前提是「有活跃券商桥」
 *   （`sync/__init__.py::_subscribe_to_qmt` 无桥直接 return），于是未连券商时
 *   五档**恒空** —— 而五档本身可从无需券商的公开行情源取到。
 *   现在面板走「WS 优先 + `GET /market/quote` 兜底」：
 *     - WS 无推送但 REST 有五档 ⇒ **必须**渲染出五档（把兜底删掉，第 1 条必红）；
 *     - WS 有五档 ⇒ 优先用 WS（把 `live` 的深度让 REST 覆盖掉，第 3 条必红）；
 *     - 两边都没有 ⇒ 说「行情不可用」+ 给出路，**不得**再说「行情通道未连接」
 *       （那是把成因单一归因到一条已被兜底覆盖的通道，第 2 条必红）。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    marketApi: { ...actual.marketApi, l2: vi.fn(), quote: vi.fn() },
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
const quote = (marketApi as unknown as { quote: ReturnType<typeof vi.fn> }).quote;

beforeEach(() => {
  l2.mockReset();
  quote.mockReset();
  quote.mockRejectedValue(new Error("no mock"));
  useQuotesStore.setState({ quotes: {}, refs: {}, socketState: "idle", lastSeq: 0 });
});

afterEach(cleanup);

describe("OrderBookPanel · WS 优先 + 行情接口兜底（不得只认 WS 推送）", () => {
  it("WS 不推送时也要靠 REST 兜底渲染出五档（否则未连券商 ⇒ 恒空）", async () => {
    // 通道 idle（无券商桥的典型形态）：WS 一条都不推
    useQuotesStore.setState({ socketState: "idle", quotes: {} });
    quote.mockResolvedValue({
      code: "600519.SH", price: 1680.5, source: "eltdx",
      bids: [{ price: 1680.5, volume: 12 }, { price: 1680.0, volume: 30 }],
      asks: [{ price: 1681.0, volume: 8 }, { price: 1681.5, volume: 21 }],
    });
    const { container } = render(<OrderBookPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("五档盘口"));

    const txt = container.textContent || "";
    expect(txt, "兜底拿到五档却没渲染出来").toContain("1681.00");
    expect(txt, "兜底拿到五档却没渲染出来").toContain("1680.50");
    expect(txt).toContain("行情接口"); // 标注数据来自接口而非实时推送
  });

  it("两边都拿不到：说「行情不可用」并给出路，不得把成因单一归因到「通道未连接」", async () => {
    useQuotesStore.setState({ socketState: "closed", quotes: {} });
    quote.mockRejectedValue(new Error("本地行情源未就绪"));
    const { container } = render(<OrderBookPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("行情不可用"));

    const txt = container.textContent || "";
    expect(txt).toContain("本地行情源未就绪");
    expect(txt).toContain("去连接"); // 券商快照同样带五档，是可行的出路
    // ★ 旧形态：REST 兜底已覆盖「没连券商」，再说「通道未连接」就是错误的单一归因
    expect(txt).not.toContain("行情通道未连接");
  });

  it("WS 有五档时优先用 WS：REST 的深度不得覆盖实时推送", async () => {
    useQuotesStore.setState({
      socketState: "open",
      quotes: {
        "600519.SH": {
          code: "600519.SH", price: 11.11, source: "broker",
          bids: [{ price: 11.05, volume: 5 }],
          asks: [{ price: 11.15, volume: 6 }],
        },
      },
    });
    quote.mockResolvedValue({
      code: "600519.SH", price: 9.9, source: "eltdx",
      bids: [{ price: 9.9, volume: 1 }],
      asks: [{ price: 9.95, volume: 2 }],
    });
    const { container } = render(<OrderBookPanel code="600519.SH" />);
    await waitFor(() => expect(container.textContent).toContain("五档盘口"));

    const txt = container.textContent || "";
    expect(txt, "WS 的深度被 REST 覆盖了").toContain("11.05");
    expect(txt, "WS 的深度被 REST 覆盖了").toContain("11.15");
    expect(txt).not.toContain("9.90");
    expect(txt).toContain("实时推送");
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
