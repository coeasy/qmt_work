/**
 * 涨停监控池「名称」列：空串必须显式占位（2026-09-20）。
 *
 * ## 缺陷回顾
 *
 * 后端 `LimitUpMonitor.add` 用 `name or code` 兜底 ⇒ `/limitup/status` 返回
 * `{"code":"600000.SH","name":"600000.SH"}` —— 界面「名称」列**把代码当名称**。
 * 后端已改为「查不到就留空」，于是前端必须把空串渲染成 `—`。
 *
 * `?? "--"` 对 `""` **不生效**（空串既不是 null 也不是 undefined）—— 这是本项目
 * 反复踩到的坑（`ScreenPanels` 名称列、`Positions`、`RuntimeJobs` 各一次）。
 *
 * ## 为什么直接测渲染函数
 *
 * `DataTable` 走 `@tanstack/react-virtual`，jsdom 下容器高度为 0 ⇒ 一行都不渲染。
 * 因此测导出的 `nameText`（与 `ScreenPanels::resultCols` 同一套路）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { nameText } from "@/domains/trading/LimitUp";

describe("涨停池 · 名称空值必须显式占位（2026-09-20）", () => {
  it("空串 → —（这正是后端改「查不到留空」之后的默认情形）", () => {
    expect(nameText("")).toBe("—");
  });

  it("undefined / null → —", () => {
    expect(nameText(undefined)).toBe("—");
    expect(nameText(null)).toBe("—");
  });

  it("纯空白 → —（后端 normalize 过，但前端不能假设）", () => {
    expect(nameText("   ")).toBe("—");
  });

  it("真实名称原样透传（含首尾空格要去掉）", () => {
    expect(nameText("浦发银行")).toBe("浦发银行");
    expect(nameText(" 贵州茅台 ")).toBe("贵州茅台");
  });
});

describe("涨停池 · 名称渲染不得回退成代码（源码级守卫）", () => {
  const SRC = readFileSync(
    resolve(__dirname, "../src/domains/trading/LimitUp.tsx"),
    "utf8",
  );

  it("名称列与池标签都走 nameText", () => {
    // 列定义
    expect(SRC).toContain('render: (r) => nameText(r.name)');
    // 池内联标签
    expect(SRC).toContain("{nameText(p.name)}");
  });

  it("不得出现旧的 `r.name ?? \"--\"`（对空串无效）", () => {
    expect(SRC).not.toContain('r.name ?? "--"');
  });

  it("不得把名称直接裸渲染（空串会渲染成空白单元格）", () => {
    expect(SRC).not.toContain("{p.name}");
  });
});
