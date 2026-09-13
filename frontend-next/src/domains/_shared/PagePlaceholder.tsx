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
