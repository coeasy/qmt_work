import {
  cloneElement,
  isValidElement,
  useId,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
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
  // ★ 自动把 label 与控件关联起来（此前多数调用方都不传 htmlFor ⇒ label 是「孤儿」：
  //   读屏不知道这个输入框是什么、点标签也无法聚焦）。
  //   仅在控件本身**没有** id 时才注入自动 id，避免覆盖调用方显式指定的 id。
  const autoId = useId();
  const isEl = isValidElement(children);
  const childId = isEl ? (children as ReactElement<{ id?: string }>).props?.id : undefined;
  const id = htmlFor ?? childId ?? (isEl ? autoId : undefined);
  const control =
    isEl && !childId && !htmlFor
      ? cloneElement(children as ReactElement<{ id?: string }>, { id: autoId })
      : children;

  return (
    <div className={s.formRow}>
      <label className={s.formLabel} htmlFor={id}>
        {label}
      </label>
      <div className={s.formControl}>{control}</div>
    </div>
  );
}
