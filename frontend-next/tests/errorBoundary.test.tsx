import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ErrorBoundary } from "@/app/ErrorBoundary";

/**
 * P0-5：单个页面抛错不能把整个壳带走。
 *
 * 这里锁三件事：
 * ① 错误被边界接住（页面不白屏）；
 * ② 展示的是可操作信息（重试 / 复制诊断），不是一行堆栈；
 * ③ 正常组件不受影响（边界不能改变正常渲染）。
 */
function Boom(): null {
  throw new Error("boom-detail");
}

describe("ErrorBoundary", () => {
  it("捕获子组件错误并展示可操作提示", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ErrorBoundary scope="demo">
          <Boom />
        </ErrorBoundary>,
      );
      expect(screen.getByRole("alert")).toBeTruthy();
      expect(screen.getByText(/这个面板出错了/)).toBeTruthy();
      expect(screen.getByText(/boom-detail/)).toBeTruthy();
      expect(screen.getByText("重试")).toBeTruthy();
      expect(screen.getByText("复制诊断")).toBeTruthy();
    } finally {
      spy.mockRestore();
    }
  });

  it("正常子组件直接渲染，不显示错误 UI", () => {
    render(
      <ErrorBoundary scope="ok">
        <div>fine</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("fine")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
