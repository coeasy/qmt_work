import type { InputHTMLAttributes, SelectHTMLAttributes, ReactNode } from "react";
import s from "./primitives.module.css";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
  /** 数字类输入启用等宽字体 + 表格数字对齐 */
  mono?: boolean;
}

export function Input({ invalid, mono, className, ...rest }: InputProps) {
  const cls = [
    s.field,
    invalid ? s.fieldError : "",
    mono ? s.fieldMono : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return <input className={cls} {...rest} />;
}

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "children"> {
  options: SelectOption[];
  placeholder?: string;
  children?: ReactNode;
}

export function Select({ options, placeholder, className, ...rest }: SelectProps) {
  const cls = [s.select, className ?? ""].filter(Boolean).join(" ");
  return (
    <select className={cls} {...rest}>
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options.map((o) => (
        <option key={o.value} value={o.value} disabled={o.disabled}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export interface FormRowProps {
  label: string;
  children: ReactNode;
  htmlFor?: string;
}

export function FormRow({ label, children, htmlFor }: FormRowProps) {
  return (
    <div className={s.formRow}>
      <label className={s.formLabel} htmlFor={htmlFor}>
        {label}
      </label>
      <div className={s.formControl}>{children}</div>
    </div>
  );
}
