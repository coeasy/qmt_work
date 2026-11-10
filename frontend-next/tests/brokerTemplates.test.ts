import { describe, expect, it } from "vitest";
import {
  ACCESS_MODE_KEYS,
  QUICK_TEMPLATES,
  availabilityKeyForMode,
  buildAccessModeOptions,
  defaultAccessMode,
  hasRealtimeSession,
  needsBridgeParams,
} from "@/domains/system/Brokers";
import type { ConnectorInfo } from "@/services/api/broker";

/**
 * 「券商连接」页接入模式的回归锁（P0-3）。
 *
 * 2026-10-23 券商收紧后，miniQMT 外接、文件桥、Redis/ZMQ 桥全部失效
 * （策略进程禁文件读写/外网）。此前模板把「文件桥」标成推荐，用户点完、
 * 填完、提交后才失败，且界面看不到根因。
 *
 * 后来把三个历史模板改成「已停用」却**忘了补闭环入口**：模板的 hint 写着
 * 「请改用闭环（qmt.big.closed）部署」，而 UI 里根本没有这一项，默认值还停在
 * 已停用的 direct —— 指引指向一条不存在的路，不改就提交必失败的连接。
 * 本文件把「退役口径」与「闭环入口」一起钉死，防止再退回半吊子状态。
 */
function connector(key: string, patch: Partial<ConnectorInfo> = {}): ConnectorInfo {
  return {
    key,
    label: key,
    transport: "embedded",
    dialect: "bigqmt.v1",
    available: true,
    retired: false,
    retirement_notice: "",
    recommended: false,
    ...patch,
  };
}

/** 与后端 registry 同形状的正常清单：只有 qmt.big.closed 可用且推荐。 */
function healthyList(): ConnectorInfo[] {
  return [
    connector("qmt.mini", { available: false, retired: true, retirement_notice: "已关停" }),
    connector("qmt.big.direct", {
      transport: "inprocess", available: false, retired: true,
      retirement_notice: "授权串限制",
    }),
    connector("qmt.big.bridge.file", {
      transport: "file", available: false, retired: true,
      retirement_notice: "策略进程禁文件读写",
    }),
    connector("qmt.big.bridge.redis", {
      transport: "redis", available: false, retired: true, retirement_notice: "禁外网",
    }),
    connector("qmt.big.bridge.zmq", {
      transport: "zmq", available: false, retired: true, retirement_notice: "禁外网含回环",
    }),
    connector("qmt.big.closed", { recommended: true }),
    connector("ptrade.http", { transport: "http", recommended: true }),
  ];
}

describe("QUICK_TEMPLATES · 闭环入口 + 历史停用项", () => {
  it("闭环模板存在、可套用、标注推荐", () => {
    const closed = QUICK_TEMPLATES.find((t) => t.accessMode === "closed");
    expect(closed, "缺少闭环快速模板").toBeDefined();
    expect(closed?.retired).toBe(false);
    expect(closed?.label).toMatch(/闭环/);
    expect(closed?.label).toMatch(/推荐/);
    expect(closed?.hint).toMatch(/计划|部署|运行态/);
  });

  it("历史场景恰好三种，且全部标停用、不带推荐字样", () => {
    const old = QUICK_TEMPLATES.filter((t) => t.retired);
    expect(old).toHaveLength(3);
    const labels = old.map((t) => t.label);
    expect(labels.some((l) => l.includes("文件桥"))).toBe(true);
    expect(labels.some((l) => l.includes("小 QMT"))).toBe(true);
    expect(labels.some((l) => l.includes("大 QMT 直连"))).toBe(true);
    for (const t of old) {
      expect(t.label).toMatch(/停用/);
      expect(t.label).not.toMatch(/推荐|首选|默认/);
    }
  });

  it("id 唯一（避免 key 冲突导致 React 渲染抖动）", () => {
    const ids = QUICK_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("每个模板 hint 必须说明为什么不可用 / 闭环要做什么", () => {
    for (const t of QUICK_TEMPLATES) {
      expect(t.hint.trim().length, `模板 ${t.id} 的 hint 是空串`).toBeGreaterThan(10);
      if (t.retired) expect(t.hint).toMatch(/停用|关停|失败|禁/);
    }
  });

  it("模板的 accessMode 都在映射表里（不能点出一个提交不了的键）", () => {
    for (const t of QUICK_TEMPLATES) {
      expect(Object.keys(ACCESS_MODE_KEYS)).toContain(t.accessMode);
    }
  });
});

describe("接入模式映射 · 闭环必须真的可达", () => {
  it("closed 映射到 qmt.big.closed", () => {
    expect(ACCESS_MODE_KEYS.closed).toBe("qmt.big.closed");
  });

  it("direct 提交空 key，但可用性看 qmt.big.direct（两个 key 不是一回事）", () => {
    expect(ACCESS_MODE_KEYS.direct).toBe("");
    expect(availabilityKeyForMode("direct")).toBe("qmt.big.direct");
  });

  it("只有三种真桥要填 bridge_dir；闭环与直连都不要", () => {
    expect(needsBridgeParams("bridgeFile")).toBe(true);
    expect(needsBridgeParams("bridgeRedis")).toBe(true);
    expect(needsBridgeParams("bridgeZmq")).toBe(true);
    expect(needsBridgeParams("closed")).toBe(false);
    expect(needsBridgeParams("direct")).toBe(false);
  });

  it("闭环没有实时会话（不许握手、不许实时探测）", () => {
    expect(hasRealtimeSession("closed")).toBe(false);
    expect(hasRealtimeSession("direct")).toBe(true);
    expect(hasRealtimeSession("bridgeFile")).toBe(true);
  });
});

describe("buildAccessModeOptions · 后端清单是唯一真源", () => {
  it("推荐项排第一，退役项禁用且标明", () => {
    const opts = buildAccessModeOptions(healthyList());
    expect(opts[0]?.value).toBe("closed");
    expect(opts[0]?.label).toMatch(/推荐/);
    expect(opts[0]?.disabled).toBe(false);
    const retired = opts.filter((o) => o.disabled);
    expect(retired).toHaveLength(4);
    for (const o of retired) expect(o.label).toMatch(/已停用/);
  });

  it("清单加载失败时不禁用任何项（一次网络抖动不能锁死页面）", () => {
    const opts = buildAccessModeOptions([]);
    expect(opts.length).toBeGreaterThan(0);
    expect(opts.every((o) => !o.disabled)).toBe(true);
  });

  it("每个选项都能反查到可用性 key", () => {
    for (const o of buildAccessModeOptions(healthyList())) {
      expect(availabilityKeyForMode(o.value)).not.toBe("");
    }
  });
});

describe("defaultAccessMode · 绝不默认选中已停用项", () => {
  it("正常清单默认选闭环（可用且推荐）", () => {
    expect(defaultAccessMode(healthyList())).toBe("closed");
  });

  it("没有推荐项时退到第一个可用项", () => {
    const items = [
      connector("qmt.big.closed", { available: false, retired: true }),
      connector("qmt.big.direct", { transport: "inprocess" }),
    ];
    expect(defaultAccessMode(items)).toBe("direct");
  });

  it("全部停用时返回空串（界面显示空态，而不是偷偷选一个必失败项）", () => {
    const allRetired = healthyList().map((i) => ({
      ...i, available: false, retired: true, recommended: false,
    }));
    expect(defaultAccessMode(allRetired)).toBe("");
  });

  it("清单为空时返回空串", () => {
    expect(defaultAccessMode([])).toBe("");
  });
});
