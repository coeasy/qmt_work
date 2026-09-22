import { describe, expect, it, afterEach, beforeEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { KlineSyncStatus, MarketCoverage, SessionSnapshot } from "@/services/api";

/**
 * 系统类页面（系统状态 / 离线数据）的**三态诚实性**回归测试（R23）。
 *
 * 这一组锁的是一个反复出现的缺陷类：**把「读不到」冒充成「事实」**。
 * 具体有三个变体，每个都曾经真实存在于代码里：
 *
 *  ① 「加载中」被显示成「不可用」——
 *     `providers` 初值 null，请求没回来就渲染「数据源矩阵不可用」；
 *  ② 「接口不可达」与「后端未装配」被一个「或」糊在一起 ——
 *     可 `/market/kline/sync-status` 在未装配时是 **200 + {initialized:false}**，接口明明可达；
 *     两者的排查方向完全不同（查网络 vs 查装配），糊起来就是让用户白跑；
 *  ③ 「取不到统计」被显示成「尚无请求」/ 未就绪时默认成某个具体取值（`capability ?? "sector"`）——
 *     前者把「读不到」说成「没有」，后者把「还没读到」说成「查的就是板块」。
 *
 * 每条断言都同时给出**否定式**（旧文案不得出现），否则「改了文案」也能让测试变绿 ——
 * 那样护栏就不可证伪了。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    marketApi: {
      klineSyncStatus: vi.fn(),
      klineCacheStats: vi.fn(),
      coverage: vi.fn(),
      session: vi.fn(),
    },
    systemApi: {
      health: vi.fn(),
      capabilities: vi.fn(),
      dataProviders: vi.fn(),
      datahubPolicies: vi.fn(),
      sourceDiagnostics: vi.fn(),
      submitJob: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import { SystemStatus } from "@/domains/system/SystemStatus";
// eslint-disable-next-line import/first
import { OfflineData } from "@/domains/system/OfflineData";
// eslint-disable-next-line import/first
import { marketApi, systemApi } from "@/services/api";

const mockMarket = marketApi as unknown as Record<string, ReturnType<typeof vi.fn>>;
const mockSystem = systemApi as unknown as Record<string, ReturnType<typeof vi.fn>>;

const SESSION: SessionSnapshot = {
  today: "20260919",
  trading_day: true,
  active: true,
  phase: "closed",
  last_trading_day: "20260918",
  as_of: "20260918",
  next_trading_day: "20260921",
  calendar: { mode: "builtin", exact: false },
  now: "2026-09-19 10:00:00",
};

const COVERAGE: MarketCoverage = {
  period: "1d",
  adjust: "qfq",
  lookback_days: 30,
  per_day: [{ dt: "20260918", codes: 5200 }],
  provider_share: [{ provider_id: "eltdx", bars: 5200, share: 1 }],
  universe_size: null,
  latest_day: "20260918",
  latest_codes: 5200,
  days_with_data: 1,
  sync: null,
};

const PROVIDERS = {
  chain_version: 1,
  default_source_policy: "auto",
  commercial_mode: false,
  capability_chains: {},
  chain_resolved: {},
  providers: [],
  screening_ready: true,
  screening_providers: ["eltdx"],
  local_data_available: true,
};

function statusOf(over: Partial<KlineSyncStatus> = {}): KlineSyncStatus {
  return {
    initialized: true, enabled: true, sync_time: "16:00", hot: {},
    last_run_persisted: null, last_bars_run: null, ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockMarket.klineSyncStatus!.mockResolvedValue(statusOf());
  mockMarket.klineCacheStats!.mockResolvedValue({ series: 3, hit_rate: null });
  mockMarket.coverage!.mockResolvedValue(COVERAGE);
  mockMarket.session!.mockResolvedValue(SESSION);
  mockSystem.health!.mockResolvedValue({ version: "0.3.1", checks: [] });
  mockSystem.capabilities!.mockResolvedValue({ total: 0, agent_visible: 0, items: [] });
  mockSystem.dataProviders!.mockResolvedValue(PROVIDERS);
  mockSystem.datahubPolicies!.mockResolvedValue({ default: {}, topics: {} });
  mockSystem.sourceDiagnostics!.mockResolvedValue(null);
});

afterEach(cleanup);

describe("系统状态页 · 三态诚实性", () => {
  it("数据源矩阵：请求还没回来时不得断言「不可用」", async () => {
    // 永不 settle ⇒ 停在加载态
    mockSystem.dataProviders!.mockReturnValue(new Promise(() => {}));
    render(<SystemStatus />);
    expect(await screen.findByText(/正在读取数据源矩阵/)).toBeTruthy();
    // 否定式：加载中就说「不可用」是假警报
    expect(screen.queryByText(/数据源矩阵不可用/)).toBeNull();
  });

  it("数据源矩阵：失败时给出失败原因，而不是笼统的「不可用」", async () => {
    mockSystem.dataProviders!.mockRejectedValue(new Error("connect ECONNREFUSED"));
    render(<SystemStatus />);
    expect(await screen.findByText(/数据源矩阵加载失败：connect ECONNREFUSED/)).toBeTruthy();
    expect(screen.queryByText(/^数据源矩阵不可用$/)).toBeNull();
  });

  it("同步状态：initialized=false 时必须说「后端未装配」，不得说「接口不可达」", async () => {
    // 后端在未装配 market_sync 时如实返回 200 + {initialized:false} ⇒ 接口是可达的
    mockMarket.klineSyncStatus!.mockResolvedValue(statusOf({ initialized: false }));
    render(<SystemStatus />);
    expect(await screen.findByText(/后端未装配 market_sync/)).toBeTruthy();
    // 否定式：把可达的接口说成「不可达」会把用户送去查网络
    expect(screen.queryByText(/接口不可达/)).toBeNull();
  });

  it("同步状态：请求失败时不得说成「未初始化」", async () => {
    mockMarket.klineSyncStatus!.mockRejectedValue(new Error("connect ECONNREFUSED"));
    render(<SystemStatus />);
    expect(await screen.findByText(/同步状态读取失败/)).toBeTruthy();
    expect(screen.queryByText(/同步器未初始化/)).toBeNull();
  });

  it("缓存统计：读取失败时不得显示成「尚无请求」", async () => {
    mockMarket.klineCacheStats!.mockRejectedValue(new Error("500 internal"));
    render(<SystemStatus />);
    expect(await screen.findByText(/统计不可用/)).toBeTruthy();
    // 否定式：「尚无请求」是在断言一个我们并不知道的事实
    expect(screen.queryByText("尚无请求")).toBeNull();
  });

  it("失败溯源：未就绪时不得默认显示成 sector", async () => {
    mockSystem.sourceDiagnostics!.mockResolvedValue(null);
    render(<SystemStatus />);
    // 等一个确定渲染出来的锚点，确保树已稳定
    expect(await screen.findByText("数据源失败溯源")).toBeTruthy();
    expect(screen.queryByText("sector")).toBeNull();
  });
});

describe("离线数据页 · 三态诚实性", () => {
  it("同步状态失败时不得说成「同步器未初始化」", async () => {
    mockMarket.klineSyncStatus!.mockRejectedValue(new Error("connect ECONNREFUSED"));
    render(<OfflineData />);
    expect(await screen.findByText(/同步状态读取失败/)).toBeTruthy();
    // 否定式：失败与「未装配」是两回事，不能糊成一句
    expect(screen.queryByText(/同步器未初始化/)).toBeNull();
  });

  it("同步器真的未装配时，文案必须点明「接口可达」", async () => {
    mockMarket.klineSyncStatus!.mockResolvedValue(statusOf({ initialized: false }));
    render(<OfflineData />);
    expect(await screen.findByText(/后端未装配 market_sync/)).toBeTruthy();
    expect(screen.queryByText(/接口不可达/)).toBeNull();
  });

  it("缓存统计读取失败时不得显示成「尚无请求」", async () => {
    mockMarket.klineCacheStats!.mockRejectedValue(new Error("500 internal"));
    render(<OfflineData />);
    expect(await screen.findByText(/统计不可用/)).toBeTruthy();
    expect(screen.queryByText("尚无请求")).toBeNull();
  });
});
