import { describe, expect, it, vi, afterEach } from "vitest";
import { act, fireEvent, render, screen, cleanup } from "@testing-library/react";
import { ConfirmButton } from "@/design/primitives";

/**
 * ConfirmButton 回归测试。
 *
 * 锁的是用户反馈原话「不能点击自动就删除了」这条产品不变量：
 * **列表行里的删除，一次点击绝不生效。**
 *
 * 修复前的真实缺陷是两件事叠在一起：
 *   ① 按钮 `opacity: 0`、`row:hover` 才浮现 ⇒ 在光标正下方凭空出现；
 *   ② CSS Grid 列数少写一列 ⇒ 「×」溢出压在**下一行**上
 *      （点第 N+1 行实际删掉第 N 行，用户完全无法建立因果）。
 * ② 属于样式、测不到；① 与「一次点击不生效」是可测的，钉死在这里。
 */

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ConfirmButton · 一次点击绝不删除", () => {
  it("点 1 次：不执行，文案切成待确认", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton onConfirm={onConfirm}>删除</ConfirmButton>);

    fireEvent.click(screen.getByRole("button"));

    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("button").textContent).toBe("确认删除");
  });

  it("点 2 次：第 2 次才真正执行", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton onConfirm={onConfirm}>删除</ConfirmButton>);

    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    fireEvent.click(btn);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    // 执行后必须复原，否则按钮会一直停在「确认删除」态等着被误点
    expect(btn.textContent).toBe("删除");
  });

  it("超时自动复原：待确认态不会一直留着等人误点", () => {
    vi.useFakeTimers();
    const onConfirm = vi.fn();
    render(<ConfirmButton onConfirm={onConfirm} timeoutMs={3000}>删除</ConfirmButton>);

    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    expect(btn.textContent).toBe("确认删除");

    act(() => {
      vi.advanceTimersByTime(3001);
    });
    expect(btn.textContent).toBe("删除");

    // 复原后必须重新走两段，不能「一次点击就补上刚才那一下」
    fireEvent.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("按 Esc 立即复原", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton onConfirm={onConfirm}>删除</ConfirmButton>);

    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { key: "Escape" });

    expect(btn.textContent).toBe("删除");
    fireEvent.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("失焦复原：鼠标移开后按钮不能还停在危险态", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton onConfirm={onConfirm}>删除</ConfirmButton>);

    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    fireEvent.blur(btn);

    expect(btn.textContent).toBe("删除");
    fireEvent.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("★ 点删除不触发表格行的 onClick（否则「删一行」会顺带「选中该行」）", () => {
    const onConfirm = vi.fn();
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <ConfirmButton onConfirm={onConfirm}>删除</ConfirmButton>
      </div>,
    );

    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    fireEvent.click(btn);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("disabled 时不进入待确认态", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton onConfirm={onConfirm} disabled>
        删除
      </ConfirmButton>,
    );

    const btn = screen.getByRole("button");
    fireEvent.click(btn);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(btn.textContent).toBe("删除");
  });

  it("卸载后定时器不再触发（不对已卸载组件 setState）", () => {
    vi.useFakeTimers();
    const onConfirm = vi.fn();
    const { unmount } = render(<ConfirmButton onConfirm={onConfirm}>删除</ConfirmButton>);

    fireEvent.click(screen.getByRole("button"));
    unmount();

    // 卸载后推进时间不应抛错（React 18 下是 warning，这里主要防回归成 error）
    expect(() => {
      act(() => {
        vi.advanceTimersByTime(5000);
      });
    }).not.toThrow();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
