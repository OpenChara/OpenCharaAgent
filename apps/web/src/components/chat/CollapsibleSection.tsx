/* A Profile-pane section whose body folds away under its header. Matches the deck's
 * section look (the uppercase .dsec h4 + the Prow `›` chevron), the chevron rotating
 * down when open. Used for tasks / skills / memory; the aspiration stays always-open. */
import { useState, type ReactNode } from "react";

export function CollapsibleSection({
  title,
  defaultOpen = true,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="dsec">
      <button
        type="button"
        className={`dsec-head${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <h4>{title}</h4>
        <span className="dsec-chev">›</span>
      </button>
      {open && <div className="dsec-body">{children}</div>}
    </section>
  );
}
