/* WorldBookEditor — a structured per-entry editor for a card's world book
 * (character_book). Each entry is a small card: a constant/keyword pill toggle,
 * a keyword chip-input, and a content box that auto-grows to fit its text;
 * entries can be added, removed, and reordered with up/down controls (which work
 * with mouse, touch, AND keyboard — unlike drag, which silently dies on touch).
 * A toolbar offers "add entry" and an AI generate/expand action
 * (card.generate_worldbook, wired by the parent).
 *
 * The editor is CONTROLLED: it holds no entry state of its own (the parent owns it
 * so edits survive a tab switch). Edits spread over the original entry object, so
 * passthrough fields (secondary_keys, selective, comment…) are never dropped.
 *
 * Binding UI rule: the generate action shows a working spinner; reorder/add/remove
 * are optimistic (they flip the parent state immediately). */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import type { WorldEntryFull } from "../../lib/cards";

function emptyEntry(): WorldEntryFull {
  return { keys: [], content: "", constant: false, enabled: true };
}

export function WorldBookEditor({
  entries,
  editable,
  onChange,
  onGenerate,
  genBusy = false,
  badge,
}: {
  entries: WorldEntryFull[];
  editable: boolean;
  onChange: (entries: WorldEntryFull[]) => void;
  /** Wired by the parent (it holds hub + the card context). Mode is chosen here:
   *  "fresh" when the world is empty, "expand" when it already has entries. */
  onGenerate?: (mode: "fresh" | "expand") => void;
  genBusy?: boolean;
  /** Optional activation badge (e.g. 下次启动生效) shown in the toolbar. */
  badge?: React.ReactNode;
}) {
  const t = useT();

  const patch = (i: number, p: Partial<WorldEntryFull>) =>
    onChange(entries.map((e, j) => (j === i ? { ...e, ...p } : e)));
  const remove = (i: number) => onChange(entries.filter((_, j) => j !== i));
  const add = () => onChange([...entries, emptyEntry()]);
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= entries.length) return;
    const next = entries.slice();
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  const genMode: "fresh" | "expand" = entries.length ? "expand" : "fresh";

  return (
    <div className="wb-editor">
      <div className="wb-toolbar">
        {editable && (
          <button type="button" className="btn soft sm" onClick={add}>
            ＋ {t("wb-add")}
          </button>
        )}
        {editable && onGenerate && (
          <button
            type="button"
            className="btn soft sm wb-gen"
            disabled={genBusy}
            onClick={() => onGenerate(genMode)}
          >
            {genBusy ? <span className="spin" /> : "✦ " + t(genMode === "expand" ? "wb-expand" : "wb-gen")}
          </button>
        )}
        <div className="grow" />
        {badge}
      </div>

      {entries.length === 0 ? (
        <div className="wb-empty">{t("wb-empty")}</div>
      ) : (
        <div className="wb-list">
          {entries.map((e, i) => (
            <EntryRow
              key={i}
              entry={e}
              editable={editable}
              isFirst={i === 0}
              isLast={i === entries.length - 1}
              onPatch={(p) => patch(i, p)}
              onRemove={() => remove(i)}
              onMoveUp={() => move(i, -1)}
              onMoveDown={() => move(i, 1)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function EntryRow({
  entry,
  editable,
  isFirst,
  isLast,
  onPatch,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  entry: WorldEntryFull;
  editable: boolean;
  isFirst: boolean;
  isLast: boolean;
  onPatch: (p: Partial<WorldEntryFull>) => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  const t = useT();
  const [kin, setKin] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const keys = entry.keys || [];

  // Rows are keyed by index, so reordering/removing reuses this instance for a
  // DIFFERENT logical entry — which would leak the in-progress keyword input
  // (kin) onto it. Clear kin whenever a different entry lands in this slot (its
  // content or key-count shifts); typing a keyword changes neither, so kin is
  // preserved while you type.
  useEffect(() => { setKin(""); }, [entry.content, keys.length]);

  // Auto-grow the content box to fit its text, so a multi-paragraph entry is
  // fully visible without an internal scrollbar or a manual drag-resize. Runs on
  // mount (tab open) and whenever the content changes.
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }, [entry.content]);

  const addKeys = (raw: string) => {
    const parts = raw
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
    setKin("");
    if (parts.length) onPatch({ keys: [...keys, ...parts] });
  };

  return (
    <div className="wb-entry">
      <div className="wb-head">
        {editable && (
          <div className="wb-reorder">
            <button
              type="button"
              className="wb-move"
              disabled={isFirst}
              title={t("wb-move-up")}
              onClick={onMoveUp}
            >
              ▲
            </button>
            <button
              type="button"
              className="wb-move"
              disabled={isLast}
              title={t("wb-move-down")}
              onClick={onMoveDown}
            >
              ▼
            </button>
          </div>
        )}
        <button
          type="button"
          className={"cv-st " + (entry.constant ? "const" : "kw")}
          onClick={() => editable && onPatch({ constant: !entry.constant })}
          title={t("wb-type-tip")}
          disabled={!editable}
        >
          {t(entry.constant ? "cv-world-const" : "cv-world-kw")}
        </button>
        <div className="wb-keys">
          {keys.map((k, ki) => (
            <span className="wb-key" key={ki}>
              {k}
              {editable && (
                <button
                  type="button"
                  className="wb-key-x"
                  title={t("wb-key-del")}
                  onClick={() => onPatch({ keys: keys.filter((_, x) => x !== ki) })}
                >
                  ×
                </button>
              )}
            </span>
          ))}
          {editable && (
            <input
              className="wb-key-in"
              value={kin}
              placeholder={keys.length ? "" : t("wb-key-ph")}
              onChange={(e) => {
                const v = e.target.value;
                if (/[,，]/.test(v)) addKeys(v);
                else setKin(v);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addKeys(kin);
                } else if (e.key === "Backspace" && !kin && keys.length) {
                  onPatch({ keys: keys.slice(0, -1) });
                }
              }}
              onBlur={() => kin.trim() && addKeys(kin)}
            />
          )}
        </div>
        {editable && (
          <button type="button" className="wb-del" title={t("wb-del")} onClick={onRemove}>
            ✕
          </button>
        )}
      </div>
      <textarea
        ref={taRef}
        className="wb-content"
        value={entry.content}
        placeholder={t("wb-content-ph")}
        readOnly={!editable}
        onChange={(e) => onPatch({ content: e.target.value })}
      />
    </div>
  );
}
