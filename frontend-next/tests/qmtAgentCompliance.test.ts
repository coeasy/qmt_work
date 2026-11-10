import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * 「部署成功 ≠ 产物可用」的呈现锁（2026-10-23 新规）。
 *
 * 背景：`/qmt-agent/bundle` 与 `/qmt-agent/deploy` 产出的是**文件桥**形态的
 * agent（`bridge_dir` + `req/resp` + `events.ndjson`）。新规关闭策略沙箱的本地
 * 文件读写之后，这条链路在真机上**跑不起来**：每拍 `open()` 抛
 * `Forbidden FileIO`，被主循环 try/except 吞掉，外部看到的正是
 * 「策略在跑但没有任何事件」。
 *
 * 如果界面只说「已部署」，用户会一路走到盘前才发现没下单 —— 这是本仓最忌讳的
 * 假绿灯（不是崩溃，是**看起来正常**）。所以这里钉住三件事：
 *
 *   1. 后端两个端点都必须把 `compliance` 随产物返回（结论放在调用方拿得到的地方）；
 *   2. 类型声明里 `ok` 与 `usable` 是**两个轴**，不能合并；
 *   3. 面板在 `usable === false` 时必须同时改 banner 文案 + 出常驻说明块 ——
 *      只改一处，用户都可能正好没看见。
 */

const SRC = resolve(__dirname, "..", "src");
const DEPLOY_TSX = readFileSync(resolve(SRC, "domains/system/QmtAgentDeploy.tsx"), "utf8");
const AGENT_TS = readFileSync(resolve(SRC, "services/api/qmtAgent.ts"), "utf8");
const ROUTE_PY = readFileSync(
  resolve(__dirname, "..", "..", "backend", "app", "routes", "qmt_agent.py"),
  "utf8",
);

describe("后端契约 · compliance 随产物返回", () => {
  it("声明了合规画像的构造点（单一来源，不在两处各写一份）", () => {
    expect(ROUTE_PY).toMatch(/def _bridge_compliance\(\)/);
    expect(ROUTE_PY).toContain('"regulation_cutoff": "2026-10-23"');
    expect(ROUTE_PY).toContain('"usable": False');
  });

  it("/bundle 与 /deploy **两个**端点都带 compliance（少一个就有一条路径在说谎）", () => {
    const bundleBlock = ROUTE_PY.slice(ROUTE_PY.indexOf('@router.get("/bundle")'));
    const deployBlock = ROUTE_PY.slice(ROUTE_PY.indexOf('@router.post("/deploy")'));
    expect(bundleBlock.slice(0, 1200)).toContain("_bridge_compliance()");
    expect(deployBlock.slice(0, 2600)).toContain("_bridge_compliance()");
  });

  it("理由指向真实失效机制（写「不兼容」等于没说）", () => {
    expect(ROUTE_PY).toContain("Forbidden FileIO");
  });
});

describe("API 客户端 · ok 与 usable 是两个轴", () => {
  it("声明了 QmtAgentCompliance 且字段齐备", () => {
    expect(AGENT_TS).toMatch(/export interface QmtAgentCompliance\s*\{/);
    for (const field of ["mode", "regulation_cutoff", "usable", "reason", "next_action"]) {
      expect(AGENT_TS).toContain(field);
    }
  });

  it("bundle 与 deploy 结果都声明了 compliance（可选：旧后端不返回时不能崩）", () => {
    expect(AGENT_TS).toMatch(/compliance\?:\s*QmtAgentCompliance/);
    const hits = AGENT_TS.match(/compliance\?:\s*QmtAgentCompliance/g) || [];
    expect(hits.length).toBeGreaterThanOrEqual(2);
  });
});

describe("部署面板 · 不可用时必须两处都提示", () => {
  it("banner 文案在不可用时追加说明（用户可能只看这一行）", () => {
    expect(DEPLOY_TSX).toContain("const unusable = res.compliance && !res.compliance.usable;");
    expect(DEPLOY_TSX).toMatch(/不可用，见下方说明/);
  });

  it("常驻说明块只在 usable === false 时出现（可用时不该吓人）", () => {
    expect(DEPLOY_TSX).toMatch(
      /lastDeploy\?\.compliance && !lastDeploy\.compliance\.usable/,
    );
  });

  it("说明块给出 reason 与 next_action（只说不能用等于把人堵死）", () => {
    expect(DEPLOY_TSX).toContain("lastDeploy.compliance.reason");
    expect(DEPLOY_TSX).toContain("lastDeploy.compliance.next_action");
    expect(DEPLOY_TSX).toContain("lastDeploy.compliance.regulation_cutoff");
  });

  it("用警告色而不是错误色（部署确实成功了，说「失败」也是假话）", () => {
    expect(DEPLOY_TSX).toContain("var(--warning-dim)");
    expect(DEPLOY_TSX).toContain("var(--warning)");
  });
});
