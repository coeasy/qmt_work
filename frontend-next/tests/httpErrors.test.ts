import { describe, expect, it, afterEach, vi } from "vitest";

/**
 * http.ts 错误文案回归测试。锁两条**归因纪律**（本项目反复强调，且极易被回退）：
 *
 *  ① 网络层失败**不能**把浏览器英文原文（`Failed to fetch`）抛给用户。
 *     这是后端没起来 / 端口被占时最常见的一条路径，用户看到英文既看不懂也无从下手。
 *
 *  ② HTTP 503 **不等于**「没连券商」。后端 503 也可能来自「仍在启动」「数据源未就绪」。
 *     替后端猜原因会把排查方向彻底带偏 —— 与后端
 *     `services/market/aggregates._unavailable` 是同一条纪律。
 *
 * ③ 反过来说：后端**给了**准确原因时，必须原样透传，不许被兜底文案覆盖。
 */

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const mockFetch = (impl: () => Promise<unknown> | unknown) => {
  const fn = vi.fn(impl as never);
  vi.stubGlobal("fetch", fn);
  return fn;
};

const response = (init: { ok: boolean; status: number; body?: string }) => ({
  ok: init.ok,
  status: init.status,
  text: async () => init.body ?? "",
});

describe("http · 错误文案", () => {
  it("① 连不上后端：显示中文，绝不出现浏览器英文原文", async () => {
    mockFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    const { http } = await import("@/services/http");

    let msg = "";
    try {
      await http.get("/market/etfs", { retry: 0 });
    } catch (e) {
      msg = (e as Error).message;
    }
    console.log("   实测文案 =", msg);
    expect(msg).not.toContain("Failed to fetch");
    expect(msg).toMatch(/无法连接后端服务/);
    expect(msg).toMatch(/系统状态/); // 必须给出下一步，而不只是报错
  });

  it("② 裸 503：不替后端断言「未连接券商」", async () => {
    mockFetch(() => Promise.resolve(response({ ok: false, status: 503 })));
    const { http } = await import("@/services/http");

    let msg = "";
    try {
      await http.get("/market/sectors", { retry: 0 });
    } catch (e) {
      msg = (e as Error).message;
    }
    console.log("   实测文案 =", msg);
    expect(msg).not.toMatch(/未连接券商客户端/);
    expect(msg).toMatch(/503/); // 事实要保留，方便排查
  });

  it("③ 后端给了准确原因：原样透传，不被兜底文案覆盖", async () => {
    const reason = "板块榜获取失败：当前无任何数据源声明该能力（券商源无此接口）";
    mockFetch(() =>
      Promise.resolve(
        response({ ok: true, status: 200, body: JSON.stringify({ code: 503, message: reason }) }),
      ),
    );
    const { http } = await import("@/services/http");

    let msg = "";
    try {
      await http.get("/market/sectors", { retry: 0 });
    } catch (e) {
      msg = (e as Error).message;
    }
    console.log("   实测文案 =", msg);
    expect(msg).toBe(reason);
  });
});
