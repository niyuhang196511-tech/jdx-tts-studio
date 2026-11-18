import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Loader2, X } from "lucide-react";
import { Children, cloneElement, isValidElement, useId } from "react";
import type { ButtonHTMLAttributes, ReactElement, ReactNode } from "react";

export function IconButton({
  label,
  children,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button
          className={`icon-btn ${className}`}
          aria-label={label}
          {...props}
        >
          {children}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="tooltip" sideOffset={7}>
          {label}
          <Tooltip.Arrow />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
export function Button({
  children,
  busy,
  variant = "default",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  busy?: boolean;
  variant?: "default" | "primary" | "danger" | "ghost";
}) {
  return (
    <button
      className={`btn btn-${variant} ${className}`}
      {...props}
      disabled={busy || props.disabled}
    >
      {busy && <Loader2 size={15} className="spin" />}
      {children}
    </button>
  );
}
export function Modal({
  title,
  description,
  children,
  open,
  onClose,
  wide = false,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  open: boolean;
  onClose: () => void;
  wide?: boolean;
}) {
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content className={`modal ${wide ? "modal-wide" : ""}`} {...(!description ? { "aria-describedby": undefined } : {})}>
          <div className="modal-head">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              {description && (
                <Dialog.Description>{description}</Dialog.Description>
              )}
            </div>
            <Dialog.Close asChild>
              <button className="icon-btn" aria-label="关闭">
                <X size={18} />
              </button>
            </Dialog.Close>
          </div>
          <div className="modal-body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function Field({
  label,
  children,
  hint,
  className = "",
}: {
  label: string;
  children: ReactNode;
  hint?: string;
  className?: string;
}) {
  const id = useId();
  let assigned = false;
  const decorate = (nodes: ReactNode): ReactNode => Children.map(nodes, node => {
    if (!isValidElement(node)) return node;
    const element = node as ReactElement<{ id?: string; children?: ReactNode; "aria-label"?: string; "aria-labelledby"?: string }>;
    if (typeof element.type === "string" && ["input", "select", "textarea"].includes(element.type)) {
      const first = !assigned;
      assigned = true;
      return cloneElement(element, {
        ...(!element.props["aria-label"] ? { "aria-labelledby": `${id}-label` } : {}),
        ...(first && !element.props.id ? { id } : {}),
      });
    }
    if (element.props.children) return cloneElement(element, { children: decorate(element.props.children) });
    return element;
  });
  return (
    <div className={`field ${className}`}>
      <label id={`${id}-label`} htmlFor={id}>{label}</label>
      {decorate(children)}
      {hint && <small>{hint}</small>}
    </div>
  );
}
export function Empty({
  icon,
  title,
  action,
}: {
  icon: ReactNode;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <h3>{title}</h3>
      {action}
    </div>
  );
}
export function StatusDot({
  state,
}: {
  state: "ok" | "warn" | "error" | "idle";
}) {
  return <i className={`status-dot status-${state}`} />;
}
