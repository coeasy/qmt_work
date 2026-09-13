import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  accountApi,
  alertApi,
  algoApi,
  limitupApi,
  marketApi,
  reconcileApi,
  signalApi,
  systemApi,
  tradeApi,
  webhookApi,
} from "@/services/api";
import { ApiError } from "@/services/http";

/**
 * 后端契约回归测试。
 *
 * 存在意义：本项目的前端曾因「字段名/路径与后端不一致」出现过多处静默失效
 * （precheck 读 res.ok 而后端返回 allowed；algo 传 side 而后端读 direction；
 * batch/cancel 传 order_ids 而后端读 items；limitup 传数组而后端收单只 code）。
 * 这些错误在类型检查下不会暴露 —— 只有对着真实路由契约断言才能挡住。
 *
 * 做法：拦截 global.fetch，断言实际发出的 URL 与 body。
 */

interface Captured {
  url: string;
  method: string;
  body: unknown;
}

let captured: Captured[] = [];

function mockFetch(data: unknown = {}, status = 200, code = 0): void {
  global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    captured.push({
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? (JSON.parse(String(init.body)) as unknown) : undefined,
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => JSON.stringify({ code, message: code === 0 ? "ok" : "boom", data }),
    } as unknown as Response;
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  captured = [];
  mockFetch({});
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** 取最后一次请求 */
const last = (): Captured => {
  const c = captured[captured.length - 1];
  if (!c) throw new Error("没有捕获到请求");
  return c;
};

describe("REST 基址与鉴权", () => {
  it("所有请求带 /api/v1 前缀", async () => {
    await tradeApi.orders();
    expect(last().url.startsWith("/api/v1/")).toBe(true);
  });
});

describe("交易域契约", () => {
  it("precheck 走 POST /trade/precheck，且返回字段是 allowed（不是 ok）", async () => {
    mockFetch({ allowed: false, reason: "超限" });
    const res = await tradeApi.precheck({
      code: "000001.SZ",
      direction: "buy",
      volume: 100,
      price: 10,
    });
    expect(last().url).toContain("/trade/precheck");
    expect(last().method).toBe("POST");
    // 关键：后端字段名为 allowed —— 若前端类型退回 ok，这里会失败
    expect(res.allowed).toBe(false);
    expect(res.reason).toBe("超限");
  });

  it("positions 的查询参数是 symbol，不是 conn_id", async () => {
    await tradeApi.positions("600519.SH");
    const url = last().url;
    expect(url).toContain("/trade/positions");
    expect(url).toContain("symbol=600519.SH");
    expect(url).not.toContain("conn_id=");
  });

  it("positions 不带参数时不发送空查询串", async () => {
    await tradeApi.positions();
    expect(last().url).toBe("/api/v1/trade/positions");
  });

  it("条件单提交用 trigger_price（不是 trigger_value）", async () => {
    await tradeApi.createCondition({
      code: "000001.SZ",
      side: "buy",
      trigger_type: "gte",
      trigger_price: 12.5,
      volume: 100,
    });
    const body = last().body as Record<string, unknown>;
    expect(last().url).toContain("/trade/conditions");
    expect(body.trigger_price).toBe(12.5);
    expect(body).not.toHaveProperty("trigger_value");
  });

  it("signal.setMode 只接受 live/paper/dry_run", async () => {
    await signalApi.setMode("dry_run");
    expect(last().body).toEqual({ mode: "dry_run" });
  });

  it("signal.submit 的字段是 side（不是 direction）", async () => {
    await signalApi.submit({ code: "000001.SZ", side: "sell", volume: 200, price: 9.9 });
    const body = last().body as Record<string, unknown>;
    expect(body.side).toBe("sell");
    expect(body).not.toHaveProperty("direction");
  });
});

describe("算法与涨停契约", () => {
  it("algo.submit 用 direction + algo（不是 side + algo_type）", async () => {
    await algoApi.submit({
      code: "000001.SZ",
      direction: "buy",
      volume: 1000,
      algo: "vwap",
      duration: 300,
      slices: 5,
    });
    const body = last().body as Record<string, unknown>;
    expect(last().url).toContain("/algo/submit");
    expect(body.direction).toBe("buy");
    expect(body.algo).toBe("vwap");
    expect(body).not.toHaveProperty("side");
    expect(body).not.toHaveProperty("algo_type");
  });

  it("涨停池是单只增删：addPool 发 {code}，removePool 走 query code=", async () => {
    await limitupApi.addPool("600519.SH");
    expect(last().body).toEqual({ code: "600519.SH" });

    await limitupApi.removePool("600519.SH");
    expect(last().method).toBe("DELETE");
    expect(last().url).toContain("code=600519.SH");
  });
});

describe("账户域批量契约", () => {
  it("batch/order 发 {orders:[...]}（不是 {conn_ids, items}）", async () => {
    await accountApi.batchOrder([
      { conn_id: "c1", code: "000001.SZ", direction: "buy", volume: 100, price: 10 },
    ]);
    const body = last().body as Record<string, unknown>;
    expect(last().url).toContain("/account/batch/order");
    expect(Array.isArray(body.orders)).toBe(true);
    expect(body).not.toHaveProperty("items");
  });

  it("batch/cancel 发 {items:[{conn_id,order_id}]}（不是 {conn_ids, order_ids}）", async () => {
    await accountApi.batchCancel([{ conn_id: "c1", order_id: "o1" }]);
    const body = last().body as Record<string, unknown>;
    expect(last().url).toContain("/account/batch/cancel");
    expect(Array.isArray(body.items)).toBe(true);
    expect(body).not.toHaveProperty("order_ids");
  });
});

describe("自动化域契约", () => {
  it("alerts 批量删除的 body 键是 ids（不是 rids）", async () => {
    await alertApi.batchDelete([1, 2, 3]);
    const body = last().body as Record<string, unknown>;
    expect(last().url).toContain("/alerts/rules/batch-delete");
    expect(body.ids).toEqual([1, 2, 3]);
    expect(body).not.toHaveProperty("rids");
  });

  it("webhooks 批量删除的 body 键是 ids", async () => {
    await webhookApi.batchDelete([7]);
    const body = last().body as Record<string, unknown>;
    expect(body.ids).toEqual([7]);
    expect(body).not.toHaveProperty("sids");
  });

  it("reconcile 用 POST /reconcile（不是 /reconcile/run）", async () => {
    await reconcileApi.run();
    expect(last().url).toContain("/reconcile");
    expect(last().url).not.toContain("/reconcile/run");
    expect(last().method).toBe("POST");
  });

  it("WAL 统计走 /reconcile/wal/stats", async () => {
    await reconcileApi.walStats();
    expect(last().url).toContain("/reconcile/wal/stats");
  });
});

describe("系统域契约", () => {
  it("运行时配置走 /config/runtime（不是 /config）", async () => {
    await systemApi.config();
    expect(last().url).toContain("/config/runtime");
  });

  it("配置回滚的 body 键是 id（不是 version）", async () => {
    await systemApi.rollbackConfig(42);
    expect(last().body).toEqual({ id: 42 });
  });

  it("调度创建字段是 kind（不是 job_kind）", async () => {
    await systemApi.createSchedule({ kind: "system.eod", cron: "0 15 * * 1-5" });
    const body = last().body as Record<string, unknown>;
    expect(body.kind).toBe("system.eod");
    expect(body).not.toHaveProperty("job_kind");
  });

  it("API Key 批量删除的 body 键是 ids", async () => {
    await systemApi.batchDeleteApiKeys([3]);
    expect(last().body).toEqual({ ids: [3] });
  });

  it("审计校验走 /audit/verify", async () => {
    await systemApi.auditVerify(100);
    expect(last().url).toContain("/audit/verify");
    expect(last().url).toContain("limit=100");
  });
});

describe("行情域契约", () => {
  it("kline 的复权参数名是 adj（不是 adjust）", async () => {
    await marketApi.kline("000001.SZ", "1d", 250, "qfq");
    const url = last().url;
    expect(url).toContain("/market/kline");
    expect(url).toContain("adj=qfq");
    expect(url).not.toContain("adjust=");
  });

  it("选股走 /market/screen 且 conditions 必填", async () => {
    await systemApi.capabilities();
    captured = [];
    const { screenApi } = await import("@/services/api");
    await screenApi.run({ conditions: '{"and":[]}', limit: 50 });
    expect(last().url).toContain("/market/screen");
    expect(last().url).toContain("conditions=");
  });
});

describe("零 mock 契约：503 绝不被粉饰为成功", () => {
  it("HTTP 503 抛 ApiError 且 brokerUnavailable=true", async () => {
    mockFetch(null, 503, 503);
    await expect(tradeApi.orders()).rejects.toBeInstanceOf(ApiError);
    try {
      await tradeApi.orders();
    } catch (e) {
      const err = e as ApiError;
      expect(err.brokerUnavailable).toBe(true);
      expect(err.status).toBe(503);
    }
  });

  it("HTTP 200 但 code!==0 同样抛错（不得当作成功）", async () => {
    mockFetch({ detail: "x" }, 200, 400);
    await expect(tradeApi.orders()).rejects.toBeInstanceOf(ApiError);
  });

  it("503 的错误文案包含连接引导", async () => {
    mockFetch(null, 503, 503);
    try {
      await tradeApi.positions();
      throw new Error("应当抛错");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).message.length).toBeGreaterThan(0);
    }
  });
});
