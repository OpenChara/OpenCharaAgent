/* DeckModal — the modal shell, a React port of app.js:897 openModal/closeModal.
 * Renders `.modal-layer.open > .modal-box.<variant>`; backdrop-click + Escape
 * close (app.js:909-910). Variant mirrors openModal's flags: "sheet" / "wide"
 * (the two-step wake + card editor) / "cardview" (the fixed-height editor box).
 * Namespaced Deck* so it never collides with a future shared Modal.
 *
 * Dialog semantics: role="dialog" aria-modal, initial focus moves INTO the box,
 * Tab is trapped inside it, and the previously-focused element gets focus back
 * on close — the minimal WAI-ARIA modal contract. */

import { useEffect, useRef, type ReactNode, type CSSProperties } from "react";

export type DeckModalVariant = "default" | "sheet" | "wide" | "cardview";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function DeckModal({
  open,
  variant = "default",
  onClose,
  style,
  children,
}: {
  open: boolean;
  variant?: DeckModalVariant;
  onClose: () => void;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  // Latest-onClose ref so the trap effect (keyed on `open` only) never re-runs —
  // parents pass inline closures, and re-running would re-steal focus each render.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    // Initial focus: the box itself (tabIndex=-1) — safe for every content shape
    // (never pops a mobile keyboard by auto-focusing a text field).
    boxRef.current?.focus();
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (ev.key !== "Tab") return;
      const box = boxRef.current;
      if (!box) return;
      const els = Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (!els.length) {
        ev.preventDefault();
        box.focus();
        return;
      }
      const first = els[0];
      const last = els[els.length - 1];
      const active = document.activeElement;
      const inside = box.contains(active);
      if (ev.shiftKey && (!inside || active === first || active === box)) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && (!inside || active === last)) {
        ev.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [open]);

  if (!open) return null;
  const boxClass =
    "modal" +
    (variant === "sheet" || variant === "wide" || variant === "cardview" ? " sheet" : "") +
    (variant === "wide" || variant === "cardview" ? " wide" : "") +
    (variant === "cardview" ? " cardview" : "");
  return (
    <div className="modal-layer open" onClick={(ev) => ev.target === ev.currentTarget && onClose()}>
      <div className={boxClass} style={style} role="dialog" aria-modal="true" ref={boxRef} tabIndex={-1}>
        {children}
      </div>
    </div>
  );
}
