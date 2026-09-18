import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, type ButtonProps } from "./Button";
import s from "./primitives.module.css";

export interface ConfirmButtonProps
  extends Omit<ButtonProps, "onClick" | "children" | "variant"> {
  /** 第二次点击（确认）时执行 —— 只有它才会真正删除 */
  onConfirm: () => void;
  /** 常态文案，如「删除」 */
  children: ReactNode;
  /** 待确认态文案，默认「确认删除」 */
  confirmText?: ReactNode;
  /** 待确认态的自动复原时间（毫秒），默认 3000 */
  timeoutMs?: number;
  /** 待确认态使用的按钮变体，默认 `danger` */
  armedVariant?: ButtonProps["variant"];
  variant?: ButtonProps["variant"];
}

/**
 * 两段式确认按钮 —— 列表行内删除的**唯一**正确写法。
 *
 * ## 为什么需要它（真实缺陷，不是洁癖）
 *
 * 列表行里放一个「点一下就直接删」的按钮，配合两种常见写法会变成**误删机器**：
 *   ① 按钮 `opacity: 0`、`row:hover` 才浮现 —— 按钮**在光标下面凭空出现**，
 *      用户本来要点行（选中/查看），手指一落就删掉了；
 *   ② 按钮命中区很小（十几 px）且紧贴可点区域 —— 想点行边缘也会打到它。
 * 实测反馈原话是「不能点击自动就删除了」。
 *
 * ## 为什么不用 `window.confirm` / `ConfirmModal`
 *
 * 列表里逐条删除是高频、低风险操作，每次弹窗会让人烦躁到直接点「确认」，
 * 反而训练出「闭眼确认」的坏习惯；而模态框还会打断列表滚动位置。
 * 两段式内联确认的代价是**多一次点击**，收益是**不可能误删**，且不打断视线。
 * 真正高风险的操作（下单、撤单、批量删除）仍应走 `ConfirmModal`。
 *
 * ## 行为
 *
 * - 第 1 次点击 → 进入待确认态（文案换成 `confirmText`、变 `danger` 色、加外环）
 * - 第 2 次点击 → 执行 `onConfirm()`
 * - `timeoutMs` 内无第二次点击 → 自动复原（避免「留在待确认态被误点」）
 * - 失焦 / 按 Esc → 立即复原
 */
export function ConfirmButton({
  onConfirm,
  children,
  confirmText = "确认删除",
  timeoutMs = 3000,
  armedVariant = "danger",
  variant = "ghost",
  size = "sm",
  ...rest
}: ConfirmButtonProps) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const disarm = () => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    setArmed(false);
  };

  // 卸载时必须清掉定时器，否则会在已卸载组件上 setState
  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

  return (
    <Button
      {...rest}
      size={size}
      variant={armed ? armedVariant : variant}
      className={[rest.className ?? "", armed ? s.armed : ""].filter(Boolean).join(" ")}
      aria-label={armed ? String(confirmText) : undefined}
      onClick={(e) => {
        // 典型用法是在可点击的表格行里：点删除绝不能顺带触发「选中该行」。
        // 因此这里一律 stopPropagation，调用方不必每行都记得写。
        e.stopPropagation();
        if (!armed) {
          setArmed(true);
          if (timer.current !== null) clearTimeout(timer.current);
          timer.current = setTimeout(disarm, timeoutMs);
          return;
        }
        disarm();
        onConfirm();
      }}
      onBlur={disarm}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          disarm();
        }
      }}
    >
      {armed ? confirmText : children}
    </Button>
  );
}
