import { describe, expect, it } from "vitest";
import { extractQuotes } from "@/stores/quotes";
import type { Quote, WsMessage } from "@/shared/types";

/**
 * WS 行情帧解析回归测试。
 *
 * 这块曾经是「行情通道已连接，但界面价格永远是 --」的真根因：
 * 后端有两种帧形状（快照用 `quotes` 键、增量用 `data.items`），
 * 而前端只认「data 是数组/对象」，两种都解析不出来，且**不报错**。
 * 因此把四种形状与畸形输入全部钉死。
 */

const q = (code: string, price: number): Quote => ({
  code,
  price,
  last: price,
} as Quote);

function frame(partial: Record<string, unknown>): WsMessage {
  return partial as unknown as WsMessage;
}

describe("extractQuotes · 帧形状", () => {
  it("全量快照帧：quotes 键是 { CODE: Quote }", () => {
    const out = extractQuotes(
      frame({
        type: "snapshot",
        seq: 1,
        quotes: { "000001.SZ": q("000001.SZ", 11.7), "600519.SH": q("600519.SH", 1500) },
      }),
    );
    expect(out.map((x) => x.code).sort()).toEqual(["000001.SZ", "600519.SH"]);
    expect(out.find((x) => x.code === "000001.SZ")?.price).toBe(11.7);
  });

  it("增量广播帧：data.items 是数组", () => {
    const out = extractQuotes(
      frame({ type: "quotes", seq: 2, data: { items: [q("000001.SZ", 11.8)] } }),
    );
    expect(out).toHaveLength(1);
    expect(out[0]?.code).toBe("000001.SZ");
    expect(out[0]?.price).toBe(11.8);
  });

  it("data 直接是数组（兼容旧形态）", () => {
    const out = extractQuotes(frame({ type: "quotes", data: [q("600519.SH", 1499)] }));
    expect(out).toHaveLength(1);
    expect(out[0]?.code).toBe("600519.SH");
  });

  it("data 是 { CODE: Quote }（兼容形态）", () => {
    const out = extractQuotes(
      frame({ type: "quotes", data: { "399006.SZ": q("399006.SZ", 2400) } }),
    );
    expect(out).toHaveLength(1);
    expect(out[0]?.code).toBe("399006.SZ");
  });

  it("快照里缺 code 的条目用键名补全", () => {
    const out = extractQuotes(
      frame({ type: "snapshot", quotes: { "000300.SH": { price: 3900 } } }),
    );
    expect(out[0]?.code).toBe("000300.SH");
  });

  it("其他类型帧一律返回空（不误吞事件）", () => {
    expect(extractQuotes(frame({ type: "order", data: { items: [q("X", 1)] } }))).toEqual([]);
    expect(extractQuotes(frame({ type: "pong" }))).toEqual([]);
  });
});

describe("extractQuotes · 畸形输入不抛错", () => {
  it("空快照 / 空 items", () => {
    expect(extractQuotes(frame({ type: "snapshot", quotes: {} }))).toEqual([]);
    expect(extractQuotes(frame({ type: "quotes", data: { items: [] } }))).toEqual([]);
    expect(extractQuotes(frame({ type: "quotes" }))).toEqual([]);
  });

  it("★不变量：items 里没有 code 的脏数据被丢弃", () => {
    const out = extractQuotes(
      frame({
        type: "quotes",
        data: { items: [q("000001.SZ", 11.7), { price: 1 }, null, "x"] },
      }),
    );
    expect(out).toHaveLength(1);
    expect(out[0]?.code).toBe("000001.SZ");
  });

  it("quotes 是数组而非对象时不当快照处理", () => {
    expect(extractQuotes(frame({ type: "snapshot", quotes: [q("A", 1)] }))).toEqual([]);
  });
});
