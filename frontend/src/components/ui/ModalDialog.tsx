import { useEffect, useId, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "../../lib/cn";

export interface ModalDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  /** When true, sets `closedby="any"` with pointerdown+pointerup outside fallback */
  lightDismiss?: boolean;
  hideHeader?: boolean;
  className?: string;
  children: ReactNode;
}

/**
 * Native HTML `<dialog>` modal following modern-web-guidance (`dialog`):
 * - Opened via `showModal()` (never `open` attribute, no redundant `role="dialog"` or `aria-modal`,
 *   no `overflow: hidden` on `html`/`body`).
 * - Always wired with `aria-labelledby` pointing to an `<h1>` heading.
 * - Resets `dialog.returnValue = ""` on `close` so stale values never leak.
 * - Supports `closedby="any"` with `pointerdown`+`pointerup` bounding-rect outside fallback.
 */
export function ModalDialog({
  open,
  onClose,
  title,
  subtitle,
  lightDismiss = false,
  hideHeader = false,
  className,
  children,
}: ModalDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (open) {
      if (!el.open) {
        try {
          el.showModal();
        } catch {
          /* ignore if already modal */
        }
      }
    } else if (el.open) {
      el.close();
    }
  }, [open]);

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    const handleClose = () => {
      el.returnValue = "";
      onCloseRef.current();
    };
    el.addEventListener("close", handleClose);
    return () => el.removeEventListener("close", handleClose);
  }, []);

  /* Fallback for browsers where `closedBy` is not on HTMLDialogElement.prototype */
  useEffect(() => {
    const el = dialogRef.current;
    if (!el || !lightDismiss) return;
    if (typeof HTMLDialogElement !== "undefined" && "closedBy" in HTMLDialogElement.prototype) {
      return;
    }
    let pressedOutside = false;
    const isOutside = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    const onPointerDown = (e: PointerEvent) => {
      pressedOutside = el.open && isOutside(e);
    };
    const onPointerUp = (e: PointerEvent) => {
      if (pressedOutside && el.open && isOutside(e)) {
        el.close();
      }
      pressedOutside = false;
    };
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointerup", onPointerUp);
    return () => {
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointerup", onPointerUp);
    };
  }, [lightDismiss]);

  const closedByAttr = lightDismiss
    ? ({ closedby: "any" } as Record<string, string>)
    : ({ closedby: "closerequest" } as Record<string, string>);

  return (
    <dialog ref={dialogRef} aria-labelledby={titleId} className={cn("modal-dialog", className)} {...closedByAttr}>
      {hideHeader ? (
        <h1 id={titleId} className="sr-only">
          {title}
        </h1>
      ) : (
        <header className="flex items-start justify-between gap-3 border-b border-hairline px-5 py-3.5">
          <div className="min-w-0">
            <h1 id={titleId} className="m-0 text-[15px] font-semibold leading-snug text-ink">
              {title}
            </h1>
            {subtitle ? <p className="m-0 mt-0.5 text-xs text-muted">{subtitle}</p> : null}
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm -mr-1 shrink-0"
            aria-label="关闭对话框"
            onClick={() => dialogRef.current?.close()}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>
      )}
      {open ? children : null}
    </dialog>
  );
}
