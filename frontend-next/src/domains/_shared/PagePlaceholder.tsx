import type { ComponentType } from "react";
import { Badge, Panel } from "@/design/primitives";
import type { PageProps } from "@/app/routes";
import s from "./placeholder.module.css";

/**
 * 占位页。
 *
 * 存在意义：后端能力已就绪但前端页面尚未实现时，**显式**展示该页的后端契约，
 * 而不是渲染一个看起来能用、实际空壳的界面。这样：
 * 1. 能力可达性清晰（对照 /capabilities 逐条核对）
 * 2. 后续实现者能直接看到需要对接的端点
 * 3. 用户不会误判功能缺失
 *
 * ⚠️ **"全仓库零引用"是刻意的**，不是死代码：它是 `app/routes.tsx` 中
 * `status: "planned"` 机制的组成部分（`shell/MenuBar` 会为 planned 渲染
 * 「待实现」角标）。当前 43 个页面全部为 done/partial，故暂无人调用。
 * 第 23 轮审计已明确判定为「既定扩展点而非缺陷」并保留 —— 后续审计勿再
 * 将其当作孤儿逻辑重复上报；除非同时决定移除 `status: "planned"` 机制。
 */
export function makePlaceholder(
  title: string,
  endpoints: string[],
  note: string,
): ComponentType<PageProps> {
  return function Placeholder() {
    return (
      <div className={s.wrap}>
        <Panel title={title} padded>
          <div className={s.row}>
            <Badge tone="warning">待实现</Badge>
            <span className={s.note}>{note}</span>
          </div>
          <div className={s.sectionTitle}>后端契约（已就绪）</div>
          <ul className={s.list}>
            {endpoints.map((e) => (
              <li key={e} className={s.item}>
                <code className={s.code}>{e}</code>
              </li>
            ))}
          </ul>
        </Panel>
      </div>
    );
  };
}
