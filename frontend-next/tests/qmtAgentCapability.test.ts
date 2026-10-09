import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * 「大 QMT agent 诊断面板」的能力真相回归锁（2026-10-09）。
 *
 * 背景：真机日志里 agent 已明确报出 `异常项=['quote_call']`（行情实调失败），
 * 但界面「诊断」结果一切正常 —— 因为面板只渲染 bundle / config / 心跳，
 * **从不看自检结论**。用户盯着永远空白的行情面板猜原因。
 *
 * 三条必须钉住的事实呈现：
 *   1. **自检结论**要显示，且必须区分「未上报」（旧 agent 没这个字段）与
 *      「未通过」——把 undefined 当 false 会让老版本永远显示红灯；
 *   2. **下单能力 / 行情能力**要分别显示，并按 `expected_in_mode` 区分
 *      「本模式预期不可用（黄）」与「真故障（红）」；
 *   3. 不可用时要给出 `reason`（后端已把根因/出路写在里面）。
 */

const SRC = resolve(__dirname, "..", "src");
const DEPLOY_TSX = readFileSync(resolve(SRC, "domains/system/QmtAgentDeploy.tsx"), "utf8");
const AGENT_TS = readFileSync(resolve(SRC, "services/api/qmtAgent.ts"), "utf8");

describe("QmtAgentDeploy · 自检结论三态", () => {
  it("probe_ok === undefined 必须渲染成「未上报」而不是「未通过」", () => {
    expect(DEPLOY_TSX).toMatch(/probe_ok === undefined/);
    expect(DEPLOY_TSX).toContain("未上报");
  });

  it("通过 / 未通过 两种终态都要有", () => {
    expect(DEPLOY_TSX).toContain("通过");
    expect(DEPLOY_TSX).toContain("未通过");
  });

  it("未通过时要列出异常项（否则用户只知道「有问题」不知道「哪里」）", () => {
    expect(DEPLOY_TSX).toContain("probe_bad_steps");
    expect(DEPLOY_TSX).toContain("异常项");
  });
});

describe("QmtAgentDeploy · 下单/行情能力分列", () => {
  it("两个能力行都在（合并成一条会让「不能下单」与「没行情」分不开）", () => {
    expect(DEPLOY_TSX).toContain("下单能力");
    expect(DEPLOY_TSX).toContain("行情能力");
  });

  it("能力行按 expected_in_mode 三态着色（预期不可用不该报红）", () => {
    expect(DEPLOY_TSX).toMatch(/expected_in_mode/);
    // 预期不可用 → warning；真故障 → danger
    expect(DEPLOY_TSX).toMatch(/"warning"/);
    expect(DEPLOY_TSX).toMatch(/"danger"/);
  });

  it("不可用时展示后端给出的 reason（含根因与出路）", () => {
    expect(DEPLOY_TSX).toMatch(/cap\.reason|\.reason/);
  });
});

describe("QmtAgent · 类型契约声明了消费的字段", () => {
  it("QmtAgentCapability 声明 available / reason / expected_in_mode", () => {
    expect(AGENT_TS).toMatch(/interface QmtAgentCapability/);
    expect(AGENT_TS).toMatch(/expected_in_mode/);
    expect(AGENT_TS).toMatch(/reason/);
  });

  it("诊断结果声明 capabilities（不声明就取不到后端字段）", () => {
    expect(AGENT_TS).toMatch(/capabilities\?/);
  });

  it("心跳声明 probe_bad_steps", () => {
    expect(AGENT_TS).toMatch(/probe_bad_steps\?/);
  });
});
