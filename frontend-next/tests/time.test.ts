import { describe, expect, it } from "vitest";
import { fmtDate, fmtDateTime, toTimestamp } from "@/shared/time";

/**
 * K 线时间解析回归测试。
 *
 * 背景：后端 `time` 字段按数据源不同有多种格式，早期实现只认 `YYYY-MM-DD`，
 * 遇到 eltdx 的 `20260817` 会解析失败并兜底为 `Date.now()`，导致**所有 K 线
 * 共用同一个时间戳**——X 轴标签全部显示同一天。这类问题类型检查完全无法发现，
 * 只能靠用例钉住，因此每个已知来源格式都要有一条。
 *
 * 断言一律用 `new Date(y, m, d)` 构造期望值：与实现同为「本地时间」，
 * 因此用例在任何时区下都成立（用 ISO 串写期望值会在非 GMT+8 环境假失败）。
 */

describe("toTimestamp · 各数据源真实格式", () => {
  it("eltdx / local_store 日线：YYYYMMDD（紧凑无分隔符）", () => {
    expect(toTimestamp("20260817")).toBe(new Date(2026, 7, 17).getTime());
  });

  it("eltdx 分钟线：YYYYMMDDHHMMSS", () => {
    expect(toTimestamp("20260817093000")).toBe(new Date(2026, 7, 17, 9, 30, 0).getTime());
  });

  it("akshare：YYYY-MM-DD", () => {
    expect(toTimestamp("2026-08-17")).toBe(new Date(2026, 7, 17).getTime());
  });

  it("optional_sources：YYYY-MM-DD HH:MM:SS", () => {
    expect(toTimestamp("2026-08-17 14:35:12")).toBe(new Date(2026, 7, 17, 14, 35, 12).getTime());
  });

  it("public_sources：YYYY/MM/DD", () => {
    expect(toTimestamp("2026/08/17")).toBe(new Date(2026, 7, 17).getTime());
  });

  it("ISO 8601 带 T 与秒", () => {
    expect(toTimestamp("2026-08-17T09:30:00")).toBe(new Date(2026, 7, 17, 9, 30, 0).getTime());
  });

  it("epoch 秒（10 位）", () => {
    expect(toTimestamp("1789267780")).toBe(1789267780 * 1000);
  });

  it("epoch 毫秒（13 位）", () => {
    expect(toTimestamp("1789267780000")).toBe(1789267780000);
  });
});

describe("toTimestamp · 时区安全", () => {
  it("纯日期串按本地时间解析，不回退到 UTC 前一天", () => {
    // 若误用 new Date("2026-08-17")，在 GMT+8 会得到 8/16 16:00Z，日期错一天
    const ts = toTimestamp("2026-08-17");
    const d = new Date(ts);
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(7); // 8 月
    expect(d.getDate()).toBe(17);
  });

  it("紧凑格式同样落在本地零点", () => {
    const d = new Date(toTimestamp("20260817"));
    expect(d.getHours()).toBe(0);
    expect(d.getDate()).toBe(17);
  });
});

describe("toTimestamp · 原始缺陷回归", () => {
  it("不同日期必须得到不同时间戳（原缺陷：全部塌缩为 Date.now()）", () => {
    const a = toTimestamp("20260817");
    const b = toTimestamp("20260818");
    expect(a).not.toBe(b);
    expect(b - a).toBe(24 * 3600 * 1000);
  });

  it("eltdx 紧凑格式不得等于当前时间", () => {
    expect(toTimestamp("20260817")).not.toBe(Date.now());
  });

  it("一整段日线序列应严格递增且跨越多天", () => {
    const raw = ["20260817", "20260818", "20260819", "20260820", "20260821"];
    const ts = raw.map(toTimestamp);
    expect(new Set(ts).size).toBe(raw.length); // 无重复
    for (let i = 1; i < ts.length; i++) {
      expect(ts[i]!).toBeGreaterThan(ts[i - 1]!);
    }
    const days = (ts[ts.length - 1]! - ts[0]!) / 86400000;
    expect(days).toBe(4);
  });
});

describe("toTimestamp · 异常输入", () => {
  it("无法识别时返回 NaN（不再兜底为当前时间）", () => {
    expect(Number.isNaN(toTimestamp("不是时间"))).toBe(true);
    expect(Number.isNaN(toTimestamp(""))).toBe(true);
    expect(Number.isNaN(toTimestamp(null))).toBe(true);
    expect(Number.isNaN(toTimestamp(undefined))).toBe(true);
  });

  it("全空白的 bar.time 不会污染图表", () => {
    // 调用方据此过滤掉该 bar，而不是让它落到 Date.now()
    expect(Number.isNaN(toTimestamp("   "))).toBe(true);
  });
});

describe("日期格式化", () => {
  it("fmtDate 输出本地 YYYY-MM-DD", () => {
    expect(fmtDate(new Date(2026, 7, 17).getTime())).toBe("2026-08-17");
    expect(fmtDate(new Date(2026, 0, 5).getTime())).toBe("2026-01-05");
  });

  it("fmtDateTime 输出 YYYY-MM-DD HH:MM", () => {
    expect(fmtDateTime(new Date(2026, 7, 17, 9, 5).getTime())).toBe("2026-08-17 09:05");
  });

  it("非法时间戳显示占位符", () => {
    expect(fmtDate(NaN)).toBe("--");
    expect(fmtDateTime(NaN)).toBe("--");
  });
});
