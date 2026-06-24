/* WorldBookEditor — a structured per-entry editor for a card's world book
 * (character_book). Each entry is a small card: a constant/keyword pill toggle,
 * a keyword chip-input, and a multi-line content box; entries can be added,
 * removed, and reordered by dragging the ⠿ handle. A toolbar offers "add entry"
 * and an AI generate/expand action (card.generate_worldbook, wired by the parent).
 *
 * The editor is CONTROLLED: it holds no entry state of its own (the parent owns it
 * so edits survive a tab switch). Edits spread over the original entry object, so
 * passthrough fields (secondary_keys, selective, comment…) are never dropped.
 *
 * Binding UI rule: the generate action shows a working spinner; reorder/add/remove
 * are optimistic (they flip the parent state immediately). */

import { useRef, useState } from "react";
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
  const dragFrom = useRef<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);

  const patch = (i: number, p: Partial<WorldEntryFull>) =>
    onChange(entries.map((e, j) => (j === i ? { ...e, ...p } : e)));
  const remove = (i: number) => onChange(entries.filter((_, j) => j !== i));
  const add = () => onChange([...entries, emptyEntry()]);

  const drop = (to: number) => {
    const from = dragFrom.current;
    dragFrom.current = null;
    setDragOver(null);
    if (from === null || from === to) return;
    const next = entries.slice();
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
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
              over={dragOver === i}
              onPatch={(p) => patch(i, p)}
              onRemove={() => remove(i)}
              onDragStart={() => (dragFrom.current = i)}
              onDragOver={(ev) => {
                ev.preventDefault();
                if (dragOver !== i) setDragOver(i);
              }}
              onDrop={() => drop(i)}
              onDragEnd={() => {
                dragFrom.current = null;
                setDragOver(null);
              }}
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
  over,
  onPatch,
  onRemove,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: {
  entry: WorldEntryFull;
  editable: boolean;
  over: boolean;
  onPatch: (p: Partial<WorldEntryFull>) => void;
  onRemove: () => void;
  onDragStart: () => void;
  onDragOver: (ev: React.DragEvent) => void;
  onDrop: () => void;
  onDragEnd: () => void;
}) {
  const t = useT();
  const [kin, setKin] = useState("");
  const keys = entry.keys || [];

  const addKeys = (raw: string) => {
    const parts = raw
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
    setKin("");
    if (parts.length) onPatch({ keys: [...keys, ...parts] });
  };

  return (
    <div
      className={"wb-entry" + (over ? " over" : "")}
      onDragOver={editable ? onDragOver : undefined}
      onDrop={editable ? onDrop : undefined}
    >
      <div className="wb-head">
        {editable && (
          <span
            className="wb-handle"
            draggable
            onDragStart={onDragStart}
            onDragEnd={onDragEnd}
            title={t("wb-reorder")}
          >
            ⠿
          </span>
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
        className="wb-content"
        value={entry.content}
        placeholder={t("wb-content-ph")}
        readOnly={!editable}
        onChange={(e) => onPatch({ content: e.target.value })}
      />
    </div>
  );
}
