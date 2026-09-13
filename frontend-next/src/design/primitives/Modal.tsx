import { useEffect, type ReactNode } from "react";
import { Button } from "./Button";
import s from "./Modal.module.css";

export interface ModalProps {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** 实盘/危险操作提示条 */
  warn?: string;
}

export function Modal({ open, title, onClose, children, footer, warn }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className={s.backdrop}
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={s.modal} role="dialog" aria-modal="true">
        <div className={s.head}>
          <span className={s.title}>{title}</span>
          <button type="button" className={s.close} onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <div className={s.body}>
          {warn !== undefined && <div className={s.warnBar}>{warn}</div>}
          {children}
        </div>
        {footer !== undefined && <div className={s.foot}>{footer}</div>}
      </div>
    </div>
  );
}

export interface ConfirmModalProps {
  open: boolean;
  title: ReactNode;
  message: ReactNode;
  warn?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** 通用确认框：下单二次确认、撤单确认等复用 */
export function ConfirmModal({
  open,
  title,
  message,
  warn,
  confirmText = "确认",
  cancelText = "取消",
  danger = false,
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      warn={warn}
      footer={
        <>
          <Button onClick={onCancel} disabled={loading}>
            {cancelText}
          </Button>
          <Button
            variant={danger ? "danger" : "primary"}
            onClick={onConfirm}
            disabled={loading}
          >
            {loading ? "处理中…" : confirmText}
          </Button>
        </>
      }
    >
      {message}
    </Modal>
  );
}
