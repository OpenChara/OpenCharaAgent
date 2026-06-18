/* Select — a flat Hermes-style dropdown (NOT a native <select>, NOT a
 * skeuomorphic button): a full-width rounded trigger showing the current value +
 * a chevron, opening a flat list with a check on the selected row. Optional
 * search filters long lists; optional allowCustom lets an unmatched query be used
 * verbatim (so the provider/model boxes double as free entry). Closes on outside
 * click / Esc. Used by the Model pane's provider box + model box. */

import { useEffect, useRef, useState } from "react";
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
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const cur = options.find((o) => o.value === value);
  const ql = q.trim().toLowerCase();
  const shown = ql
    ? options.filter((o) => o.label.toLowerCase().includes(ql) || o.value.toLowerCase().includes(ql))
    : options;
  const exact = options.some((o) => o.value === q.trim());

  const choose = (v: string) => {
    onChange(v);
    setOpen(false);
    setQ("");
  };

  // group consecutive options under their `group` header (preserves input order)
  let lastGroup: string | undefined;

  return (
    <div className={"sel" + (disabled ? " disabled" : "")} ref={ref}>
      <button
        type="button"
        className={"sel-trigger" + (open ? " open" : "")}
        disabled={disabled}
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
          <div className="sel-list">
            {shown.map((o) => {
              const head = o.group && o.group !== lastGroup ? ((lastGroup = o.group), o.group) : null;
              return (
                <div key={o.value}>
                  {head && <div className="sel-group">{head}</div>}
                  <button type="button" className={"sel-item" + (o.value === value ? " on" : "")} onClick={() => choose(o.value)}>
                    <span className="sel-name">{o.label}</span>
                    {o.note && <span className="sel-note">{o.note}</span>}
                    {o.value === value && <Check size={14} className="sel-check" />}
                  </button>
                </div>
              );
            })}
            {allowCustom && ql && !exact && (
              <button type="button" className="sel-item sel-use" onClick={() => choose(q.trim())}>
                {t("model-use-typed", { q: q.trim() })}
              </button>
            )}
            {shown.length === 0 && !(allowCustom && ql) && <div className="sel-empty">{emptyText || "—"}</div>}
          </div>
        </div>
      )}
    </div>
  );
}
