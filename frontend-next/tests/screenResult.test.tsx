import { describe, expect, it, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ScreenResult, resultCols } from "@/domains/research/screen/ScreenPanels";
import { useWatchlistStore } from "@/stores/watchlist";
import type { ScreenResponse, ScreenRow } from "@/services/api";

/**
 * 选股结果区回归测试。锁住 2026-09-18 的三处优化：
 *  ① 未连接券商时显示善意提示（选股依赖行情/全市场数据，断连会静默失败）；
 *  ② 结果表补「成交量」列（后端每行已返回 volume，前端此前没展示）；
 *  ③ 每行「加自选 / 移出自选」操作（补齐「列表增」闭环，与检索/板块雷达一致）。
 *
 * 注意：结果表是虚拟化表格，jsdom 容器高度为 0 → 行不渲染，故行内交互不在此断言；
 * 「加自选」回调改用 `resultCols` 的 操作列 render 直接单测（更稳、更聚焦）。
 */

const res: ScreenResponse = {
  count: 1,
  total_scanned: 100,
  elapsed_ms: 50,
  sort_by: "score",
  conditions: {},
  results: [
    { code: "600000.SH", name: "浦发银行", close: 10.5, change_pct: 1.2, volume: 12345678, score: 0.8 },
  ],
  provenance: {},
  degraded: false,
};

afterEach(() => {
  cleanup();
  useWatchlistStore.getState().setAll(["000001.SZ", "600519.SH", "000300.SH", "399006.SZ"]);
});

describe("ScreenResult · 选股结果区优化", () => {
  it("展示成交量列头", () => {
    render(<ScreenResult res={res} busy={false} idleText="idle" emptyText="empty" />);
    // ① 成交量列头存在（之前缺失）
    expect(screen.getByText("成交量")).toBeTruthy();
  });

  it("未连接券商时显示连接提示", () => {
    render(<ScreenResult res={res} busy={false} idleText="idle" emptyText="empty" />);
    // 默认 broker store connections 为空 → 提示「未连接券商」
    expect(screen.getByText(/未连接券商/)).toBeTruthy();
  });

  it("操作列渲染加自选按钮并按 code 切换自选股", () => {
    const toggle = vi.fn();
    const cols = resultCols({}, toggle, []);
    const actCol = cols.find((c) => c.key === "act");
    expect(actCol).toBeTruthy();
    const { getByText } = render(<>{actCol!.render({ code: "600000.SH" } as ScreenRow, 0)}</>);
    const btn = getByText("加自选");
    fireEvent.click(btn);
    expect(toggle).toHaveBeenCalledWith("600000.SH");
  });

  it("已自选的代码显示移出自选", () => {
    const toggle = vi.fn();
    const cols = resultCols({}, toggle, ["600000.SH"]);
    const actCol = cols.find((c) => c.key === "act")!;
    const { getByText } = render(<>{actCol.render({ code: "600000.SH" } as ScreenRow, 0)}</>);
    expect(getByText("移出自选")).toBeTruthy();
  });
});

describe("ScreenResult · 名称缺失必须显式占位（2026-09-20）", () => {
  /**
   * 缺陷：后端在**无名称数据**时返回的是 `name: ""`，而空串既不是 null 也不是
   * undefined ⇒ 原来的 `r.name ?? "--"` 不生效，单元格渲染成**一片空白**，
   * 用户分不清「这只票没有名称数据」和「界面坏了」。
   * 铁律：缺失一律显示 `—`，绝不静默空白。
   */
  const nameCol = () => resultCols({}, () => {}, []).find((c) => c.key === "name")!;

  it("空字符串渲染成 —（不是空白）", () => {
    const col = nameCol();
    const { container } = render(<>{col.render({ code: "000333.SZ", name: "" } as ScreenRow, 0)}</>);
    expect(container.textContent).toBe("—");
  });

  it("undefined / 纯空白同样渲染成 —", () => {
    const col = nameCol();
    for (const bad of [undefined, "   "]) {
      const { container } = render(
        <>{col.render({ code: "000333.SZ", name: bad } as ScreenRow, 0)}</>,
      );
      expect(container.textContent).toBe("—");
    }
  });

  it("正常名称原样渲染", () => {
    const col = nameCol();
    const { container } = render(
      <>{col.render({ code: "000333.SZ", name: "美的集团" } as ScreenRow, 0)}</>,
    );
    expect(container.textContent).toBe("美的集团");
  });

  it("整列都无名称时给出成因说明（而非只留一片空白）", () => {
    render(
      <ScreenResult
        res={{ ...res, count: 1, results: [{ code: "000333.SZ", name: "", close: 84.4 }] }}
        busy={false}
        idleText="idle"
        emptyText="empty"
      />,
    );
    expect(screen.getByText(/未取到股票名称/)).toBeTruthy();
  });

  it("有名称时不显示该说明（避免无谓打扰）", () => {
    render(
      <ScreenResult
        res={{ ...res, count: 1, results: [{ code: "000333.SZ", name: "美的集团", close: 84.4 }] }}
        busy={false}
        idleText="idle"
        emptyText="empty"
      />,
    );
    expect(screen.queryByText(/未取到股票名称/)).toBeNull();
  });
});
