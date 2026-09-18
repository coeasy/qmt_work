import type { PageProps } from "@/app/routes";
import { FormulaPanel } from "./screen/ScreenPanels";

/**
 * 公式选股（独立页）。
 *
 * 面板实现见 `screen/ScreenPanels`（「选股工作台」共用同一份）——
 * 公式与条件树走**同一个求值内核**，两者只是「怎么写规则」的两种入口，
 * 故结果表、选项、溯源全部共用，避免同源两写而漂移。
 */
export function Formula(_props: PageProps) {
  return <FormulaPanel />;
}

export default Formula;
