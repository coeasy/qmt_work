import { describe, expect, it, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ClosedLoopNotice } from "@/domains/trading/ClosedLoopNotice";

/**
 * 新规下单形态说明（ClosedLoopNotice）的回归测试。
 *
 * 锁的是产品不变量「**不给即时反馈的假象**」：
 *
 * 2026-10-23 起，策略沙箱禁文件读写、禁一切对外网络（含 127.0.0.1 回环）。
 * 网络类违规的处置是**停模**（策略被强制停止），不是拒绝一次调用。所以
 * 「外部发指令 → 策略执行」这条实现不是慢，是不能走。
 *
 * 界面上的失败模式很具体：如果只留一个「远程买入」按钮，用户点完没反应
 * 会以为是 bug，然后反复点 —— 而真正的原因是规则变了。这组用例钉住三件事：
 *   ① 四项能力必须**逐条**出现，且「远程实时下单」被明确标为不可用；
 *   ② 延迟必须写出来（分钟级 ≠ 秒级，混写就是骗人）；
 *   ③ 必须点明本地手动下单仍然可用 —— 否则用户会以为整个交易页都废了。
 */

afterEach(() => {
  cleanup();
});

describe("ClosedLoopNotice · 远程能力的诚实边界", () => {
  it("四项能力逐条出现", () => {
    render(<ClosedLoopNotice />);
    for (const label of ["远程监控", "远程干预", "远程决策", "远程实时下单"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("「远程实时下单」被标为不可用，其余三项可用", () => {
    render(<ClosedLoopNotice />);
    // 徽标计数：3 可用 / 1 不可用 —— 用汇总徽标卡住「别哪天被悄悄改回可用」
    expect(screen.getByText("1 项不可用")).toBeTruthy();
    expect(screen.getAllByText("可用")).toHaveLength(3);
    expect(screen.getAllByText("不可用")).toHaveLength(1);
  });

  it("延迟写实：远程决策是分钟级，不是秒级", () => {
    render(<ClosedLoopNotice />);
    expect(screen.getByText("分钟级")).toBeTruthy();
    // 秒级有两条（监控 + 干预），不是三条
    expect(screen.getAllByText("秒级")).toHaveLength(2);
  });

  it("点明「停模」而不是「报错」—— 两者的处置完全不同", () => {
    render(<ClosedLoopNotice />);
    expect(screen.getAllByText(/停模/).length).toBeGreaterThan(0);
  });

  it("说明本地手动下单仍然可用", () => {
    render(<ClosedLoopNotice />);
    expect(screen.getByText(/本地手动/)).toBeTruthy();
  });

  it("紧凑模式只给一行结论，不展开能力表", () => {
    render(<ClosedLoopNotice compact />);
    expect(screen.getByRole("note")).toBeTruthy();
    expect(screen.queryByText("远程实时下单")).toBeNull();
    expect(screen.getByText(/生成计划 → 部署策略 → 查看执行/)).toBeTruthy();
  });
});
