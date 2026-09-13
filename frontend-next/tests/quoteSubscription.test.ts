import { describe, expect, it, beforeEach, vi } from "vitest";

/**
 * 订阅聚合测试 —— 本工程相对旧前端的核心修复。
 *
 * 旧行为：每个 Tab 各自 useQuotes → 打开 10 个含同一标的的 Tab 会产生 10 次订阅。
 * 新行为：以标的为键做引用计数，只有「0 → 1」才下发 subscribe，
 *        「1 → 0」才下发 unsubscribe。
 */

const mocks = vi.hoisted(() => ({
  connect: vi.fn(),
  addCodes: vi.fn(),
  removeCodes: vi.fn(),
}));

vi.mock("@/services/ws", () => ({
  quoteSocket: {
    connect: mocks.connect,
    addCodes: mocks.addCodes,
    removeCodes: mocks.removeCodes,
    onMessage: () => () => undefined,
    onState: () => () => undefined,
  },
}));

const { useQuotesStore } = await import("@/stores/quotes");

describe("行情订阅聚合（refcount）", () => {
  beforeEach(() => {
    mocks.connect.mockClear();
    mocks.addCodes.mockClear();
    mocks.removeCodes.mockClear();
    useQuotesStore.getState().clear();
  });

  it("首次订阅才下发 subscribe，并建立连接", () => {
    useQuotesStore.getState().acquire(["600519.SH"]);
    expect(mocks.connect).toHaveBeenCalledTimes(1);
    expect(mocks.addCodes).toHaveBeenCalledWith(["600519.SH"]);
  });

  it("重复订阅同一标的不会重复下发（10 个 Tab 只占 1 个服务端订阅）", () => {
    const st = useQuotesStore.getState();
    for (let i = 0; i < 10; i++) st.acquire(["600519.SH"]);

    // 只有第一次真正下发
    expect(mocks.addCodes).toHaveBeenCalledTimes(1);
    expect(mocks.addCodes).toHaveBeenCalledWith(["600519.SH"]);
    expect(useQuotesStore.getState().refs["600519.SH"]).toBe(10);
  });

  it("只有引用计数归零才退订", () => {
    const st = useQuotesStore.getState();
    st.acquire(["600519.SH"]);
    st.acquire(["600519.SH"]);
    st.acquire(["600519.SH"]);

    st.release(["600519.SH"]);
    st.release(["600519.SH"]);
    expect(mocks.removeCodes).not.toHaveBeenCalled();

    st.release(["600519.SH"]);
    expect(mocks.removeCodes).toHaveBeenCalledTimes(1);
    expect(mocks.removeCodes).toHaveBeenCalledWith(["600519.SH"]);
    expect(useQuotesStore.getState().refs["600519.SH"]).toBeUndefined();
  });

  it("多标的混合订阅只对新增标的下发", () => {
    const st = useQuotesStore.getState();
    st.acquire(["A", "B"]);
    mocks.addCodes.mockClear();

    st.acquire(["B", "C"]);
    expect(mocks.addCodes).toHaveBeenCalledWith(["C"]);
  });

  it("超额释放不会把计数压成负数", () => {
    const st = useQuotesStore.getState();
    st.acquire(["A"]);
    st.release(["A"]);
    st.release(["A"]);
    expect(useQuotesStore.getState().refs["A"]).toBeUndefined();
    expect(mocks.removeCodes).toHaveBeenCalledTimes(1);
  });

  it("setQuote 按 code 合并增量更新", () => {
    const st = useQuotesStore.getState();
    st.setQuote({ code: "600519.SH", price: 1700, name: "贵州茅台" });
    st.setQuote({ code: "600519.SH", price: 1710 });
    const q = useQuotesStore.getState().quotes["600519.SH"];
    expect(q?.price).toBe(1710);
    // 未提供的字段应保留
    expect(q?.name).toBe("贵州茅台");
  });

  it("缺少 code 的行情被忽略，不污染状态", () => {
    const st = useQuotesStore.getState();
    st.setQuote({ price: 1 } as never);
    expect(Object.keys(useQuotesStore.getState().quotes).length).toBe(0);
  });

  it("activeCodes 反映当前订阅集合", () => {
    const st = useQuotesStore.getState();
    st.acquire(["A", "B"]);
    st.release(["A"]);
    expect(useQuotesStore.getState().activeCodes()).toEqual(["B"]);
  });
});
