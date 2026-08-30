import { describe, expect, it } from "vitest";

import { getLocale, registerDict, setLocale, t } from "../i18n.js";

describe("i18n 基础框架", () => {
  it("默认中文 + 字典命中", () => {
    expect(t("common.loading")).toBe("加载中…");
  });

  it("缺省回退 key 本身（不漏翻）", () => {
    expect(t("no.such.key")).toBe("no.such.key");
  });

  it("切英文", () => {
    setLocale("en");
    expect(t("common.loading")).toBe("Loading…");
    expect(getLocale()).toBe("en");
    setLocale("zh");
  });

  it("参数插值", () => {
    registerDict("zh", { "stock.code": "代码 {code}" });
    expect(t("stock.code", { code: "600519.SH" })).toBe("代码 600519.SH");
  });

  it("非法 locale 回退 zh", () => {
    setLocale("xx");
    expect(getLocale()).toBe("zh");
  });

  it("页面标签 zh/en 全覆盖（导航迁移锚点）", () => {
    setLocale("zh");
    expect(t("page.quote")).toBe("行情分析");
    expect(t("page.trade")).toBe("手动交易");
    expect(t("page.settings")).toBe("设置");
    setLocale("en");
    expect(t("page.quote")).toBe("Quotes");
    expect(t("page.trade")).toBe("Trade");
    expect(t("page.settings")).toBe("Settings");
    // 标题
    expect(t("page.trade.title")).toBe("Manual Trading");
    expect(t("tree.行情")).toBe("Market");
    expect(t("menu.交易")).toBe("Trading");
    setLocale("zh");
  });
});
