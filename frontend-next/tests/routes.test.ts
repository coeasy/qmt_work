import { describe, expect, it } from "vitest";
import { MENU, PAGES, allPages, pageLabel } from "@/app/routes";

/**
 * 页面注册表一致性测试。
 *
 * 存在意义：旧前端有 4 个 Hub 未注册，导致约 12 个页面在生产环境**不可达**却无人发现。
 * 这组断言把「菜单项必须有对应页面」变成 CI 门禁，防止同类问题复发。
 */
describe("页面注册表", () => {
  it("菜单中每个条目都能解析到已注册页面", () => {
    const missing: string[] = [];
    for (const group of MENU) {
      for (const key of group.items) {
        if (!PAGES[key]) missing.push(`${group.label} → ${key}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("每个注册页面都至少属于一个菜单分组（不存在孤立页面）", () => {
    const inMenu = new Set(MENU.flatMap((g) => g.items));
    const orphans = Object.keys(PAGES).filter((k) => !inMenu.has(k));
    expect(orphans).toEqual([]);
  });

  it("菜单无重复条目", () => {
    const all = MENU.flatMap((g) => g.items);
    expect(new Set(all).size).toBe(all.length);
  });

  it("页面 key 与定义中的 key 字段一致", () => {
    for (const [k, def] of Object.entries(PAGES)) {
      expect(def.key).toBe(k);
    }
  });

  it("每个页面都有非空 label 与懒加载组件", () => {
    for (const def of allPages()) {
      expect(def.label.length).toBeGreaterThan(0);
      expect(def.comp).toBeTruthy();
    }
  });

  it("pageLabel 对未知 key 回退为 key 本身", () => {
    expect(pageLabel("not-exist")).toBe("not-exist");
    expect(pageLabel("trade")).toBe("手动交易");
  });

  it("六大业务域齐备", () => {
    expect(MENU.map((g) => g.key)).toEqual([
      "market",
      "research",
      "trading",
      "account",
      "automation",
      "system",
    ]);
  });

  it("已实现页面（done）达到最小覆盖集", () => {
    const done = allPages()
      .filter((p) => p.status === "done")
      .map((p) => p.key)
      .sort();
    // 核心链路必须已有真实页面：行情、下单、连接、健康
    expect(done).toContain("quote");
    expect(done).toContain("quoteboard");
    expect(done).toContain("trade");
    expect(done).toContain("brokers");
    expect(done).toContain("sysstatus");
  });

  it("方案 §5 核心流程对应页面均已实现（不再是占位）", () => {
    // 这些页面各自承载一条端到端流程的验证点：
    // 算法拆单 / 条件触发 / 涨停监控 / 多账户聚合 / 对账核销 / 告警 / 调度 / 审计
    const mustBeDone = [
      "algo",
      "conditions",
      "limitup",
      "accounts",
      "positions",
      "reconcile",
      "alerts",
      "webhooks",
      "signals",
      "runtime_jobs",
      "audit",
      "apikeys",
      "settings",
      "minutes",
      "orderbook",
      "sector_radar",
      "moneyflow",
      "etfs",
      "mktstructure",
      "screen",
      "formula",
      "search",
      // 旧 frontend 退役移植（2026-09）：成交明细 / 因子研究 / 系统日志 必须真实实现
      "deal_feed",
      "factor_hub",
      "system_log",
      // 模拟盘（2026-09 能力可达性核对补齐）：后端 6 个 /paper/* 端点此前零前端入口
      "paper",
    ];
    const notDone = mustBeDone.filter((k) => PAGES[k]?.status !== "done");
    expect(notDone).toEqual([]);
  });

  it("仅剩 1 个占位页（分仓再平衡）", () => {
    // 系统日志已由占位实现为真实 WS 事件流页（旧 frontend 退役移植），不再占位。
    // 目标持仓（target_portfolio）已按 backend /target-portfolio/* 契约实现为真实页面（210 行）。
    const planned = allPages()
      .filter((p) => p.status === "planned")
      .map((p) => p.key)
      .sort();
    expect(planned).toEqual(["rebalance"]);
  });

  it("回测页仍未开放，但因子研究已按退役决策移植回来", () => {
    // 历史决策曾把「回测」从前端移除（后端 /backtest 端点仍在，但不做页面）。
    // 2026-09 旧 frontend 退役：为达成零功能回退，因子研究（factor_hub）已从
    // 旧 frontend/hubs/FactorHub.jsx 移植到 frontend-next，故此处不再断言其缺席。
    const keys = Object.keys(PAGES);
    expect(keys).not.toContain("backtest");
    expect(keys).not.toContain("factors");
    expect(keys).not.toContain("research_factor");
    // 移植后的因子研究页必须是真实实现，而非占位
    expect(PAGES.factor_hub).toBeDefined();
    expect(PAGES.factor_hub?.status).toBe("done");
  });
});
