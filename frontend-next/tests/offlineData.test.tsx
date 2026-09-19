import { describe, expect, it, afterEach, beforeEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { KlineSyncStatus, MarketCoverage, SessionSnapshot, SyncRunRecord } from "@/services/api";

/**
 * 「离线数据」页的**诚实性**回归测试。
 *
 * 这一页存在的全部意义就是如实回答「本地数据到底同步了没有、新到哪天」，
 * 所以这里锁的不是布局，而是**四个不能含糊的区分**：
 *
 *  ① `last_run_persisted === null`（从未跑过）≠ `status: "error"`（跑了但失败）；
 *  ② `per_day` 为空（库里根本没数据）≠ 一个空表格（看起来像「还在加载」）；
 *  ③ `latest_day` 落后于数据参照日（数据不够新）必须顶出来 ——
 *     否则「昨天同步成功」会被读成「今天的数据已就绪」；
 *  ④ `stale > 0`（同步成功但数据陈旧）必须顶出来 —— 这是最容易被忽略的假成功。
 *
 * 表格行不在此断言：DataTable 是虚拟化表格，jsdom 容器高度为 0 ⇒ 行不渲染。
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
    systemApi: { submitJob: vi.fn() },
  };
});

// eslint-disable-next-line import/first
import { OfflineData } from "@/domains/system/OfflineData";
// eslint-disable-next-line import/first
import { marketApi, systemApi } from "@/services/api";

const mockMarket = marketApi as unknown as {
  klineSyncStatus: ReturnType<typeof vi.fn>;
  klineCacheStats: ReturnType<typeof vi.fn>;
  coverage: ReturnType<typeof vi.fn>;
  session: ReturnType<typeof vi.fn>;
};
const mockSystem = systemApi as unknown as { submitJob: ReturnType<typeof vi.fn> };

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

function statusOf(over: Partial<KlineSyncStatus> = {}): KlineSyncStatus {
  return { initialized: true, enabled: true, sync_time: "16:00", hot: {}, ...over };
}

function recordOf(over: Partial<SyncRunRecord> = {}): SyncRunRecord {
  return { stream: "sync.bars", last_ts: "2026-09-18T16:05:00", status: "ok", detail: {}, ...over };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockMarket.klineSyncStatus.mockResolvedValue(statusOf());
  mockMarket.klineCacheStats.mockResolvedValue({ series: 3, hit_rate: null });
  mockMarket.coverage.mockResolvedValue(COVERAGE);
  mockMarket.session.mockResolvedValue(SESSION);
});

afterEach(cleanup);

describe("离线数据页 · 诚实性", () => {
  it("从未跑过同步显示「从未跑过」而不是空白或 0", async () => {
    mockMarket.klineSyncStatus.mockResolvedValue(statusOf({ last_run_persisted: null }));
    render(<OfflineData />);
    expect(await screen.findByText("从未跑过")).toBeTruthy();
    expect(await screen.findByText(/尚未记录到任何一次同步运行/)).toBeTruthy();
  });

  it("跑了但失败要显示失败原因（与「从未跑过」可区分）", async () => {
    mockMarket.klineSyncStatus.mockResolvedValue(statusOf({
      last_run_persisted: recordOf({
        status: "error", detail: { error: "股票池为空（数据源不可用且本地无股票列表）" },
      }),
    }));
    render(<OfflineData />);
    expect(await screen.findByText("失败")).toBeTruthy();
    expect(await screen.findByText(/股票池为空/)).toBeTruthy();
  });

  it("ok 很高但数据陈旧时把 stale 顶到台面上", async () => {
    mockMarket.klineSyncStatus.mockResolvedValue(statusOf({
      last_run_persisted: recordOf({
        detail: { ok: 5153, stale: 5093, as_of_max: "20250418" },
      }),
    }));
    render(<OfflineData />);
    expect(await screen.findByText(/5093 只标的的数据/)).toBeTruthy();
    expect(await screen.findByText(/2025-04-18/)).toBeTruthy();
  });

  it("库中没有任何日线数据时给出可操作指引，而不是空表格", async () => {
    mockMarket.coverage.mockResolvedValue({
      ...COVERAGE, per_day: [], provider_share: [],
      latest_day: "", latest_codes: 0, days_with_data: 0,
    });
    render(<OfflineData />);
    expect(await screen.findByText(/本地库中没有任何日线数据/)).toBeTruthy();
    // 「可操作」的判据是**按钮真的在**（文案里提到它不算）
    expect(await screen.findByRole("button", { name: "立即同步日线" })).toBeTruthy();
  });

  it("数据未更新到最新交易日时必须提示（不许把昨天的行情当成今天的）", async () => {
    mockMarket.coverage.mockResolvedValue({ ...COVERAGE, latest_day: "20260910" });
    render(<OfflineData />);
    expect(await screen.findByText(/未更新到最新交易日/)).toBeTruthy();
  });

  it("覆盖度接口失败时显示失败原因，而不是伪装成「没有数据」", async () => {
    mockMarket.coverage.mockRejectedValue(new Error("覆盖率报表查询失败：database is locked"));
    render(<OfflineData />);
    expect(await screen.findByText(/覆盖度报表加载失败/)).toBeTruthy();
    expect(screen.queryByText(/本地库中没有任何日线数据/)).toBeNull();
  });

  it("同步状态接口失败时不显示成「未启用」", async () => {
    mockMarket.klineSyncStatus.mockRejectedValue(new Error("connect ECONNREFUSED"));
    render(<OfflineData />);
    expect(await screen.findByText(/同步状态加载失败/)).toBeTruthy();
  });

  it("skipped 归为「已跳过」而非失败", async () => {
    mockMarket.klineSyncStatus.mockResolvedValue(statusOf({
      last_run_persisted: recordOf({
        stream: "market.sync", status: "skipped",
        detail: { reason: "热表内无日线序列（从未同步过，或序列已全部滚动进冷仓）" },
      }),
    }));
    render(<OfflineData />);
    expect(await screen.findByText("已跳过")).toBeTruthy();
    expect(await screen.findByText(/热表内无日线序列/)).toBeTruthy();
  });

  it("点击「立即同步日线」提交 system.sync_bars 任务", async () => {
    mockSystem.submitJob.mockResolvedValue({ id: "job-9", status: "queued" });
    render(<OfflineData />);
    const btn = await screen.findByRole("button", { name: "立即同步日线" });
    btn.click();
    await screen.findByText(/已提交同步任务（job-9/);
    expect(mockSystem.submitJob).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "system.sync_bars" }));
  });
});
