/**
 * 撤单必须显式带 `conn_id` —— 后端「已阻止回退到其他账户」这道保险的唯一接点。
 *
 * ## 背景（2026-09-20 实测）
 *
 * 后端 `routes/trade.py::trade_cancel` 早就加了账户安全保险：
 * 指定了 `conn_id` 却取不到该连接时**报 404，绝不静默回退到 active**
 * （否则多账户下会撤错账户、或撤不到却报成功）。
 *
 * 但前端 `Positions.tsx` 调的是 `tradeApi.cancel(orderId)` —— **从不传 conn_id**
 * ⇒ 这道保险等于没接上，实际行为仍是「恒用 active 连接」。
 * 这正是本项目反复出现的「能力存在但没人调用」。
 *
 * 为什么 `DataTable` 行内断言不行：委托列表是虚拟滚动表格，jsdom 下容器高度为 0
 * ⇒ **一行都不渲染**。所以这里对**源码**做结构性断言（与 `orderFormNote.test.ts`
 * 同一套路），锁住「传参」这个事实本身。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "../src");

function read(rel: string): string {
  return readFileSync(resolve(SRC, rel), "utf8");
}

describe("撤单 · conn_id 必须真的传下去（2026-09-20）", () => {
  it("tradeApi.cancel 把 conn_id 放进请求体", () => {
    const src = read("services/api/trade.ts");
    const idx = src.indexOf('"/trade/cancel"');
    expect(idx).toBeGreaterThan(-1);
    const body = src.slice(idx, idx + 220);
    expect(body).toContain("order_id: orderId");
    expect(body).toContain("conn_id: connId");
  });

  it("Positions.tsx 撤单时传 active 连接的 conn_id", () => {
    const src = read("domains/account/Positions.tsx");
    expect(src).toContain("tradeApi.cancel(orderId, active?.conn_id");
    // 反向锁：不允许退回「只传 orderId」的旧写法
    expect(src).not.toMatch(/tradeApi\.cancel\(orderId\)/);
  });

  it("conn_id 变化会让 cancelOrder 重新生成（避免闭包捕获旧账户）", () => {
    const src = read("domains/account/Positions.tsx");
    // ⚠️ 源文件是 CRLF ⇒ 用「无换行歧义」的字面量，别写带 \n 的多行匹配。
    expect(src).toContain("[orders, active?.conn_id]");
  });
});
