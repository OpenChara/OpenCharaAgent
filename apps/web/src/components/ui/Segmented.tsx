/* Segmented — a single-select toggle group with full WAI-ARIA radiogroup
 * semantics: one tab stop (roving tabindex), Arrow Left/Right/Up/Down move AND
 * select, Home/End jump. Replaces the hand-rolled `<div className="seg">` +
 * aria-pressed button groups so every segmented control behaves identically.
 * Styling rides the existing `.seg` / `.seg button` CSS. */

import { useRef, type ReactNode } from "react";

export interface SegOption<T extends string> {
  value: T;
  label: ReactNode;
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
  ariaLabelledby,
  className,
  disabled,
}: {
  value: T;
  options: SegOption<T>[];
  onChange: (v: T) => void;
  ariaLabel?: string;
  ariaLabelledby?: string;
  className?: string;
  disabled?: boolean;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const idx = options.findIndex((o) => o.value === value);

  const focusTo = (i: number) => {
    const n = options.length;
    if (!n) return;
    const j = ((i % n) + n) % n;
    onChange(options[j].value);
    refs.current[j]?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    const cur = idx < 0 ? 0 : idx;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); focusTo(cur + 1); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); focusTo(cur - 1); }
    else if (e.key === "Home") { e.preventDefault(); focusTo(0); }
    else if (e.key === "End") { e.preventDefault(); focusTo(options.length - 1); }
  };

  return (
    <div
      className={"seg" + (className ? " " + className : "")}
      role="radiogroup"
      aria-label={ariaLabel}
      aria-labelledby={ariaLabelledby}
      onKeyDown={onKeyDown}
    >
      {options.map((o, i) => (
        <button
          key={o.value}
          ref={(el) => { refs.current[i] = el; }}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          // Roving tabindex: only the selected row (or the first, if none is
          // selected) is in the tab order; arrows move within the group.
          tabIndex={o.value === value || (idx < 0 && i === 0) ? 0 : -1}
          disabled={disabled}
          className={o.value === value ? "on" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
