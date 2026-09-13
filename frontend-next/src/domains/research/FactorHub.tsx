import { useState } from "react";
import { Tabs } from "@/design/primitives";
import type { PageProps } from "@/app/routes";
import { FactorsPanel } from "./FactorsPanel";
import { ResearchPanel } from "./ResearchPanel";
import { RegistryPanel } from "./RegistryPanel";
import s from "../domain.module.css";

/**
 * 因子研究：指标 / IC 与归因（研究深度）。
 * 移植自旧 frontend/hubs/FactorHub.jsx 的 Hub 容器；旧前端用内部 tab 切换三块，
 * 本实现复用设计令牌与 Tabs 原语，三块分别为 FactorsPanel / ResearchPanel / RegistryPanel。
 */
type HubTab = "factors" | "research" | "registry";

const TABS: Array<{ key: HubTab; label: string }> = [
  { key: "factors", label: "因子/指标" },
  { key: "research", label: "研究深度" },
  { key: "registry", label: "因子注册表" },
];

export default function FactorHub(_props: PageProps) {
  const [tab, setTab] = useState<HubTab>("factors");

  return (
    <div className={s.page} style={{ gap: "var(--sp-2)" }}>
      <Tabs
        items={TABS.map((t) => ({ key: t.key, label: t.label }))}
        value={tab}
        onChange={(k) => setTab(k as HubTab)}
      />
      {tab === "factors" && <FactorsPanel />}
      {tab === "research" && <ResearchPanel />}
      {tab === "registry" && <RegistryPanel />}
    </div>
  );
}
