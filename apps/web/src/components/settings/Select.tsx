/* Select — a flat Hermes-style dropdown (NOT a native <select>, NOT a
 * skeuomorphic button): a full-width rounded trigger showing the current value +
 * a chevron, opening a flat list with a check on the selected row. Optional
 * search filters long lists; optional allowCustom lets an unmatched query be used
 * verbatim (so the provider/model boxes double as free entry). Closes on outside
 * click / Esc. Used by the Model pane's provider box + model box.
 *
 * Accessibility: the trigger is a role=combobox with aria-expanded/haspopup; the
 * popup is a role=listbox of role=option rows; ArrowUp/Down move the active row,
 * Enter selects it, Home/End jump, Esc closes — and aria-activedescendant points
 * the screen reader at the active option. Keyboard works whether focus is on the
 * trigger or the search box (the handler lives on the root). */

import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Check } from "lucide-react";
import { useT } from "../../i18n";

export interface SelectOption {
  value: string;
  label: string;
  note?: string;
  group?: string;
}

export function Select({
  value,
  options,
  onChange,
  placeholder,
  search = false,
  allowCustom = false,
  emptyText,
  disabled = false,
}: {
  value: string;
  options: SelectOption[];
  onChange: (v: string) => void;
  placeholder?: string;
  search?: boolean;
  allowCustom?: boolean;
  emptyText?: string;
  disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const baseId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const cur = options.find((o) => o.value === value);
  const ql = q.trim().toLowerCase();
  const shown = ql
    ? options.filter((o) => o.label.toLowerCase().includes(ql) || o.value.toLowerCase().includes(ql))
    : options;
  const exact = options.some((o) => o.value === q.trim());
  const showCustom = allowCustom && !!ql && !exact;
  // The keyboard-navigable rows in render order: filtered options then the
  // optional "use typed" row. `active` indexes into this.
  const navValues = [...shown.map((o) => o.value), ...(showCustom ? [q.trim()] : [])];

  // Open at the selected row (or the top).
  useEffect(() => {
    if (!open) return;
    const sel = shown.findIndex((o) => o.value === value);
    setActive(sel >= 0 ? sel : 0);
    // only when opening — query-driven clamping is handled separately
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  // Keep active in range as the filtered list narrows.
  useEffect(() => {
    setActive((a) => Math.min(Math.max(0, a), Math.max(0, navValues.length - 1)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, options.length]);

  // Keep the active row scrolled into view as it moves. getElementById takes the id
  // literally (no CSS-selector escaping needed for useId's ":"); scrollIntoView is
  // optional-called so a non-DOM env (jsdom) that lacks it doesn't throw.
  useEffect(() => {
    if (!open) return;
    document.getElementById(`${baseId}-opt-${active}`)?.scrollIntoView?.({ block: "nearest" });
  }, [active, open, baseId]);

  const choose = (v: string) => {
    onChange(v);
    setOpen(false);
    setQ("");
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === "Escape") {
      if (open) { e.preventDefault(); setOpen(false); }
      return;
    }
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (navValues.length ? (a + 1) % navValues.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (navValues.length ? (a - 1 + navValues.length) % navValues.length : 0));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(Math.max(0, navValues.length - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (navValues[active] !== undefined) choose(navValues[active]);
    }
  };

  // group consecutive options under their `group` header (preserves input order)
  let lastGroup: string | undefined;
  const listId = `${baseId}-list`;
  const activeId = open && navValues.length ? `${baseId}-opt-${active}` : undefined;

  return (
    <div className={"sel" + (disabled ? " disabled" : "")} ref={ref} onKeyDown={onKeyDown}>
      <button
        type="button"
        className={"sel-trigger" + (open ? " open" : "")}
        disabled={disabled}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={activeId}
        onClick={() => !disabled && setOpen((v) => !v)}
      >
        <span className="sel-val">{cur ? cur.label : value || <i className="sel-ph">{placeholder}</i>}</span>
        <ChevronDown size={15} className="sel-chev" />
      </button>

      {open && (
        <div className="sel-pop">
          {search && (
            <input
              className="sel-search"
              autoFocus
              placeholder={t("model-search-ph")}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          )}
          <div className="sel-list" role="listbox" id={listId}>
            {shown.map((o, i) => {
              const head = o.group && o.group !== lastGroup ? ((lastGroup = o.group), o.group) : null;
              return (
                <div key={o.value}>
                  {head && <div className="sel-group" role="presentation">{head}</div>}
                  <button
                    type="button"
                    id={`${baseId}-opt-${i}`}
                    role="option"
                    aria-selected={o.value === value}
                    className={"sel-item" + (o.value === value ? " on" : "") + (i === active ? " active" : "")}
                    onClick={() => choose(o.value)}
                    onMouseEnter={() => setActive(i)}
                  >
                    <span className="sel-name">{o.label}</span>
                    {o.note && <span className="sel-note">{o.note}</span>}
                    {o.value === value && <Check size={14} className="sel-check" />}
                  </button>
                </div>
              );
            })}
            {showCustom && (
              <button
                type="button"
                id={`${baseId}-opt-${shown.length}`}
                role="option"
                aria-selected={false}
                className={"sel-item sel-use" + (active === shown.length ? " active" : "")}
                onClick={() => choose(q.trim())}
                onMouseEnter={() => setActive(shown.length)}
              >
                {t("model-use-typed", { q: q.trim() })}
              </button>
            )}
            {shown.length === 0 && !showCustom && <div className="sel-empty">{emptyText || "—"}</div>}
          </div>
        </div>
      )}
    </div>
  );
}
