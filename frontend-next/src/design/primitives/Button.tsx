import type { ButtonHTMLAttributes, ReactNode } from "react";
import s from "./primitives.module.css";

export type ButtonVariant =
  | "default"
  | "primary"
  | "danger"
  | "ghost"
  | "buy"
  | "sell";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md" | "lg";
  block?: boolean;
  children?: ReactNode;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  default: "",
  primary: s.btnPrimary ?? "",
  danger: s.btnDanger ?? "",
  ghost: s.btnGhost ?? "",
  buy: s.btnBuy ?? "",
  sell: s.btnSell ?? "",
};

const SIZE_CLASS: Record<"sm" | "md" | "lg", string> = {
  sm: s.btnSm ?? "",
  md: "",
  lg: s.btnLg ?? "",
};

export function Button({
  variant = "default",
  size = "md",
  block = false,
  className,
  type = "button",
  ...rest
}: ButtonProps) {
  const cls = [
    s.btn,
    SIZE_CLASS[size],
    VARIANT_CLASS[variant],
    block ? s.btnBlock : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return <button type={type} className={cls} {...rest} />;
}
