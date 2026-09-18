import type { PageProps } from "@/app/routes";
import { ConditionsPanel } from "./screen/ScreenPanels";

/**
 * 条件选股（独立页）。
 *
 * 面板实现见 `screen/ScreenPanels`（「选股工作台」共用同一份）——
 * 本页只是把它单独挂出来，供习惯单页操作 / 需要分栏对照时使用。
 */
export function Screen(_props: PageProps) {
  return <ConditionsPanel />;
}

export default Screen;
