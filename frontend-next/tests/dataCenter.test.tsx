import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

/**
 * 数据中心（`domains/system/DataCenter.tsx`）后端契约消费回归测试（R31）。
 *
 * 这一组锁的缺陷类是 **「后端算了、前端一个字没读」的载荷漂移**（TD-31 同族）：
 * `/datasets` 与 `/datasets/sources` 把 18 条数据集的**单位口径 / 游标语义 /
 * 复权 / 区间能力**、以及**源可用性**都返回了，页面却只渲染了行数与生效源 ——
 * 用户看不到「volume 是股还是手」这种**跨源单位不一致**的长期事故源。
 *
 * 更重要的是其中的**诚实性**断言：
 *  - `calendar` 的解析链恒为空（`local` 不是注册 provider），但它**真的能同步**
 *    （靠内置日历）。旧渲染逻辑会显示「无可用源」= **假告警**。
 *    新契约用 `builtin_fallback` 区分「没人能供数」与「有内置实现」。
 *  - 每条断言都给出**否定式**（旧文案 must not appear），否则改文案就能让测试变绿。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    datasetsApi: {
      list: vi.fn(),
      sources: vi.fn(),
      detail: vi.fn(),
      sync: vi.fn(),
      data: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import DataCenter from "@/domains/system/DataCenter";
// eslint-disable-next-line import/first
import { datasetsApi } from "@/services/api";

// ★ 这里必须用**具名属性**的形态而不是 `Record<string, ...>`：本仓库 tsconfig 开了
//   `noUncheckedIndexedAccess`，索引签名取出的值会带上 `| undefined`，
//   于是 `mockApi.list` 在 `beforeEach` 里报 TS18048（possibly undefined）。
const mockApi = datasetsApi as unknown as {
  list: ReturnType<typeof vi.fn>;
  sources: ReturnType<typeof vi.fn>;
  detail: ReturnType<typeof vi.fn>;
  sync: ReturnType<typeof vi.fn>;
  data: ReturnType<typeof vi.fn>;
};

function item(over: Record<string, unknown> = {}) {
  return {
    id: "ticks",
    label: "逐笔成交",
    category: "tick",
    category_label: "逐笔",
    capability: "ticks",
    chain: ["tdx"],
    store: "local_ticks",
    cursor: "event_date",
    period: "",
    supports_range: false,
    lookback: 500,
    retention_days: 7,
    cron: "20 15 * * 1-5",
    default_enabled: false,
    adjust: "",
    unit_note: "volume=手（注意：与 K 线的股不同）",
    tags: ["heavy"],
    local: { rows: 0, codes: 0, first_dt: "", last_dt: "" },
    last_sync_at: "",
    ...over,
  };
}

const CALENDAR = item({
  id: "calendar", label: "交易日历", category: "calendar",
  category_label: "日历", capability: "calendar", chain: ["broker", "local"],
  store: "exchange_calendar", cursor: "snapshot", supports_range: false,
  lookback: 320, retention_days: 0, cron: "0 8 1 * *", default_enabled: true,
  adjust: "", unit_note: "本地内置 2024-2026；券商可用时用券商日历覆盖",
  tags: [], local: { rows: 120, codes: 120, first_dt: "20240102", last_dt: "20260930" },
});

const BARS_1D = item({
  id: "bars_1d", label: "日线", category: "bars", category_label: "K 线",
  capability: "kline", chain: ["broker", "tdx", "baostock"],
  store: "local_bars", cursor: "bar_date", period: "1d",
  supports_range: true, lookback: 320, retention_days: 0,
  cron: "0 16 * * 1-5", default_enabled: true, adjust: "qfq",
  unit_note: "volume=股，amount=元", tags: ["core"],
  local: { rows: 5224000, codes: 5224, first_dt: "20100104", last_dt: "20260930" },
});

const TICKS = item();  // 默认构造的就是 ticks（volume=手、无可用源、大数据量）

const LIST = {
  total: 3,
  categories: [
    { id: "bars", label: "K 线", items: [BARS_1D] },
    { id: "tick", label: "逐笔", items: [TICKS] },
    { id: "calendar", label: "日历", items: [CALENDAR] },
  ],
};

const SOURCES = {
  broker_available: false,
  broker_note: "未检测到券商客户端；数据将以第三方源提供。",
  providers: [
    { provider: "tdx", name: "easy_tdx", active: true, dependency_available: true,
      commercial_ok: true, capabilities: ["kline", "ticks"], status: "ok" },
    { provider: "broker", name: "QMT 券商", active: false, dependency_available: false,
      commercial_ok: true, capabilities: ["kline"], status: "inactive" },
  ],
  health: {},
  per_dataset: {
    ticks: { declared: ["tdx"], resolved: [], effective_first: "",
             builtin_fallback: "", builtin_note: "" },
    // ★ 关键：解析链为空，但链里有内置源 local ⇒ 必须显示「内置」，不是「无可用源」
    calendar: { declared: ["broker", "local"], resolved: [], effective_first: "",
                builtin_fallback: "local", builtin_note: "内置（不依赖网络）" },
    // 声明链 ≠ 实际链：券商没连 ⇒ 实际降到 tdx，页面要能看出这个差异
    bars_1d: { declared: ["broker", "tdx", "baostock"], resolved: ["tdx"],
               effective_first: "tdx", builtin_fallback: "", builtin_note: "" },
  },
};

beforeEach(() => {
  mockApi.list.mockResolvedValue(LIST);
  mockApi.sources.mockResolvedValue(SOURCES);
  mockApi.detail.mockResolvedValue(BARS_1D);
  mockApi.data.mockResolvedValue({ dataset: "bars_1d", code: "", count: 0, rows: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DataCenter · 数据集契约消费", () => {
  it("渲染单位口径（跨源单位不一致是长期事故源，必须常驻可见）", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(screen.getByText("逐笔成交")).toBeTruthy());
    expect(screen.getByText(/volume=手/)).toBeTruthy();
    expect(screen.getByText(/volume=股，amount=元/)).toBeTruthy();
  });

  it("渲染游标语义与区间能力（决定「增量」到底是什么含义）", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(screen.getByText("日线")).toBeTruthy());
    expect(screen.getByText(/按 K 线日期/)).toBeTruthy();
    expect(screen.getByText(/复权:qfq/)).toBeTruthy();
    // 源不支持区间拉取 ⇒ 全量会退化，必须显式告知
    expect(screen.getAllByText(/源不支持区间拉取/).length).toBeGreaterThan(0);
    expect(screen.getByText(/整批刷新/)).toBeTruthy();
  });

  it("只在解析链真的有 provider 时显示生效源；有内置实现时不得报「无可用源」", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(screen.getByText("交易日历")).toBeTruthy());

    // calendar：解析链为空但有内置实现 ⇒ 显示内置，且**不得**出现「无可用源」
    expect(screen.getByText("local")).toBeTruthy();
    expect(screen.getByText(/内置（不依赖网络）/)).toBeTruthy();

    // ticks：既无解析链也无内置实现 ⇒ 这才是真正的「无可用源」
    expect(screen.getAllByText("无可用源").length).toBe(1);
  });

  it("声明链与实际链不同时必须并列展示（否则用户困惑「我明明配了券商优先」）", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(screen.getByText("日线")).toBeTruthy());
    expect(screen.getByText(/声明：broker,tdx,baostock → 实际：tdx/)).toBeTruthy();
  });

  it("数据源可用性矩阵必须渲染（后端算好了 providers，前端不能一个字不读）", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(screen.getByText(/easy_tdx/)).toBeTruthy());
    expect(screen.getByText(/QMT 券商/)).toBeTruthy();
    expect(screen.getByText(/未连券商（走第三方）/)).toBeTruthy();
  });

  it("本地行数按真实统计渲染（0 行显示「无数据」而不是 0 行）", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(screen.getByText("逐笔成交")).toBeTruthy());
    expect(screen.getByText(/5,224,000 行 \/ 5224 只/)).toBeTruthy();
    expect(screen.getByText("无数据")).toBeTruthy();
  });
});
