import type { ReactNode } from "react";
import s from "./primitives.module.css";

export interface TabItem<T extends string = string> {
  key: T;
  label: ReactNode;
}

export interface TabsProps<T extends string = string> {
  items: TabItem<T>[];
  value: T;
  onChange: (key: T) => void;
  className?: string;
}

/** 页内轻量 Tab（区别于外壳的工作区 Tab） */
export function Tabs<T extends string = string>({
  items,
  value,
  onChange,
  className,
}: TabsProps<T>) {
  return (
    <div className={[s.tabs, className ?? ""].filter(Boolean).join(" ")} role="tablist">
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          role="tab"
          aria-selected={it.key === value}
          className={[s.tab, it.key === value ? s.tabActive : ""]
            .filter(Boolean)
            .join(" ")}
          onClick={() => onChange(it.key)}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}
