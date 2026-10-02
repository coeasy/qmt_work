/**
 * 「账户类型」下拉选项契约（R19 第 1 轮）。
 *
 * 背景：R18 给大 QMT agent 加了 ``account_type``（→ ``opAccountType`` → 扩展
 * 12-arg passorder 签名），但前端**完全没有消费者**，探针上报的
 * ``meta().account_types`` 成了「假绿灯」：看得到、用不上。
 *
 * 本文件锁住前端这一侧的形状；「界面选项 ⊆ 后端可接受值」的**跨语言对账**
 * 在后端 `tests/test_order_account_type_wiring.py` 里做（它直接读本文件源码），
 * 两侧合起来才是完整护栏。
 */
import { describe, expect, it } from "vitest";

import { ACCOUNT_TYPE_OPTIONS } from "@/domains/trading/OrderForm";

/** 后端 canonical key（`agent_bigqmt/qmt_api.py::_ACCOUNT_TYPE_ALIASES` 的值集合）。 */
const CANONICAL = ["stock", "etf", "future", "option", "credit"];

describe("下单表单 · 账户类型", () => {
  it("提供「自动」选项且其 value 为空串（不下发字段）", () => {
    const auto = ACCOUNT_TYPE_OPTIONS.filter((o) => o.value === "");
    expect(auto).toHaveLength(1);
  });

  it("覆盖全部 5 个后端 canonical 类型，不重不漏", () => {
    const values = ACCOUNT_TYPE_OPTIONS.filter((o) => o.value !== "").map((o) => o.value);
    expect([...values].sort()).toEqual([...CANONICAL].sort());
  });

  it("每个选项都有可读中文标签", () => {
    for (const o of ACCOUNT_TYPE_OPTIONS) {
      expect(o.label.length).toBeGreaterThan(1);
      expect(o.label).toMatch(/[\u4e00-\u9fa5]|A 股|ETF/);
    }
  });

  it("不出现后端别名（futures/lof/两融 等由后端归一化，界面不重复提供）", () => {
    const values = ACCOUNT_TYPE_OPTIONS.map((o) => String(o.value));
    for (const alias of ["futures", "fund_etf", "lof", "margin", "a_stock", "options"]) {
      expect(values).not.toContain(alias);
    }
  });
});
