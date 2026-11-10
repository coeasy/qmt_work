import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * 「闭环策略运行态」在诊断面板上的呈现锁（2026-10-31）。
 *
 * 背景：新规（2026-10-23 起）禁策略进程读写文件，闭环模式下
 * `agent_status.json` / `probe_result.json` / `events.ndjson` **一个都不会生成**。
 * 旧面板只看这三份文件，于是把一条**正在正常下单**的策略显示成
 * 「agent 未运行」—— 假红灯。用户照着提示去点「运行」，反而会把一个
 * 正在执行当日仓位计划的好策略重启掉。
 *
 * 所以这里钉住四件事：
 *   1. 面板必须区分**闭环 / 文件桥**两种形态（`closed_loop` 为 null 即旧形态）；
 *   2. 闭环的心跳要**说清来源是客户端日志**，否则用户会去 bridge_dir 找一份
 *      「本来就该不存在」的文件；
 *   3. 独立进程形态必须红字明说「一单都发不出去」（这是闭环空转的第一号成因）；
 *   4. 本轮进度（已发/在途/成交/拒单/已到位）与只读/停机原因必须可见 ——
 *      「今天到底跑了没有」是用户唯一真正要问的问题。
 *
 * 与 `qmtAgentCapability.test.ts` 同风格：读源码做语义断言，不挂载组件
 * （面板依赖 http/ws 单例，挂载成本远大于它要锁的这几行事实）。
 */

const SRC = resolve(__dirname, "..", "src");
const DEPLOY_TSX = readFileSync(resolve(SRC, "domains/system/QmtAgentDeploy.tsx"), "utf8");
const AGENT_TS = readFileSync(resolve(SRC, "services/api/qmtAgent.ts"), "utf8");

describe("API 客户端 · 闭环运行态", () => {
  it("声明了 GET /qmt-agent/closed-loop（可高频轮询的运行态端点）", () => {
    expect(AGENT_TS).toContain("/qmt-agent/closed-loop");
    expect(AGENT_TS).toMatch(/closedLoop\s*:/);
  });

  it("diagnose 结果里声明了 closed_loop 字段（null = 旧文件桥形态）", () => {
    expect(AGENT_TS).toMatch(/closed_loop\?:\s*QmtAgentClosedLoopState\s*\|\s*null/);
  });

  it("状态类型带齐判定所需的字段（缺一个就会让界面「看起来正常」）", () => {
    for (const field of ["exit_code", "alive", "standalone", "orders",
                         "rejects", "settled", "done", "window_closed",
                         "readonly", "halted", "findings", "log_path"]) {
      expect(AGENT_TS).toMatch(new RegExp(`\\b${field}\\b`));
    }
  });

  it("exit_code 的语义写在注释里（0 就绪 / 2 需人工动作 / 1 环境缺失）", () => {
    expect(AGENT_TS).toContain("需人工动作");
    expect(AGENT_TS).toContain("环境缺失");
  });

  it("available=false 必须被描述成「判不出来」而不是「没问题」", () => {
    expect(AGENT_TS).toMatch(/判不出来|无法判定/);
  });
});

describe("诊断面板 · 运行形态分叉", () => {
  it("闭环与文件桥两种形态都显式标注", () => {
    expect(DEPLOY_TSX).toContain("运行形态");
    expect(DEPLOY_TSX).toContain("闭环（新规形态）");
    expect(DEPLOY_TSX).toContain("文件桥（旧形态）");
  });

  it("闭环时展示计划 id（否则「跑的是哪份计划」无从判断）", () => {
    expect(DEPLOY_TSX).toMatch(/closed_loop\.plan_id/);
  });

  it("独立进程形态给出红字与改法（改成「内置 Python」并重启）", () => {
    expect(DEPLOY_TSX).toMatch(/closed_loop\.standalone/);
    expect(DEPLOY_TSX).toContain("独立进程");
    expect(DEPLOY_TSX).toContain("内置 Python");
  });
});

describe("诊断面板 · 心跳来源必须说清", () => {
  it("心跳是**三态**：判不出来 ≠ 未运行（把「没读到」当成「没有」会让人去重启好策略）", () => {
    expect(DEPLOY_TSX).toMatch(/heartbeat\.known === false/);
    expect(DEPLOY_TSX).toContain("判不出来");
    expect(AGENT_TS).toMatch(/known\?: boolean/);
  });

  it("闭环心跳标注来源是客户端日志（不是 agent_status.json）", () => {
    expect(DEPLOY_TSX).toMatch(/closed_loop\.log_path/);
    expect(DEPLOY_TSX).toContain("客户端日志");
  });

  it("展示最后一条记录的类型与距今秒数（「多久没动」要能一眼看出）", () => {
    expect(DEPLOY_TSX).toMatch(/last_age_s/);
    expect(DEPLOY_TSX).toMatch(/last_kind/);
  });

  it("闭环下「自检未上报」要解释成形态决定，而不是故障", () => {
    expect(DEPLOY_TSX).toContain("闭环禁写盘");
  });
});

describe("诊断面板 · 本轮进度", () => {
  it("已发 / 在途 / 成交 / 拒单 四项都要在", () => {
    expect(DEPLOY_TSX).toContain("本轮进度");
    expect(DEPLOY_TSX).toMatch(/closed_loop\.orders/);
    expect(DEPLOY_TSX).toMatch(/closed_loop\.settled/);
    expect(DEPLOY_TSX).toMatch(/closed_loop\.rejects/);
    expect(DEPLOY_TSX).toMatch(/heartbeat\?\.pending/);
  });

  it("到位与否、窗口是否已收都要显式呈现（不能靠推断）", () => {
    expect(DEPLOY_TSX).toMatch(/closed_loop\.done/);
    expect(DEPLOY_TSX).toMatch(/closed_loop\.window_closed/);
    expect(DEPLOY_TSX).toContain("已到位");
    expect(DEPLOY_TSX).toContain("窗口已收");
  });

  it("只读 / 停机要有徽标 + 原因原文（只说「不交易」等于没说）", () => {
    expect(DEPLOY_TSX).toMatch(/closed_loop\.readonly/);
    expect(DEPLOY_TSX).toMatch(/closed_loop\.halted/);
    expect(DEPLOY_TSX).toContain("只读");
    expect(DEPLOY_TSX).toContain("已停机");
    expect(DEPLOY_TSX).toContain("不交易原因");
  });

  it("拒单数非零时用 danger 色（静默的拒单会让人以为「没信号」）", () => {
    expect(DEPLOY_TSX).toMatch(/rejects[\s\S]{0,120}var\(--danger\)/);
  });
});
