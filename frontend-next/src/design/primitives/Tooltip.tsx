import { useState, type ReactNode } from "react";
import s from "./primitives.module.css";

export interface TooltipProps {
  text: string;
  children: ReactNode;
}

/** 轻量 tooltip：hover 显示，无第三方依赖 */
export function Tooltip({ text, children }: TooltipProps) {
  const [show, setShow] = useState(false);
  return (
    <span
      className={s.tipWrap}
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
    >
      {children}
      {show && <span className={s.tip}>{text}</span>}
    </span>
  );
}
