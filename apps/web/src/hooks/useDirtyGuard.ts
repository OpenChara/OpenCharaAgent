/* useDirtyGuard — a confirm-before-discard guard for a modal that holds unsaved
 * edits. Three editors (CardEditor / WakeSheet / CreateFlow) had hand-rolled the
 * same dirty-ref + container onInput/onChange + guardedClose three times; this is
 * the one shape.
 *
 * Spread `dirtyProps` on the editor's content container — any field edit (the
 * contenteditable CardFields fire `input`, the controlled inputs/selects fire
 * `change`) bubbles up and flags dirty. Call `guardedClose` from the modal's
 * onClose (DeckModal Esc/backdrop) AND the Cancel button, so no close path
 * bypasses the prompt. `blocked` (e.g. () => saving || waking) refuses to close
 * mid-operation. A successful save/delete should call the raw onClose, not this. */

import { useRef } from "react";
import { useT } from "../i18n";

export function useDirtyGuard(onClose: () => void, blocked?: () => boolean) {
  const t = useT();
  const dirty = useRef(false);
  const guardedClose = () => {
    if (blocked?.()) return; // never close mid-operation
    if (dirty.current && !confirm(t("discard-edits-q"))) return;
    onClose();
  };
  const mark = () => {
    dirty.current = true;
  };
  return { guardedClose, dirtyProps: { onInput: mark, onChange: mark } };
}
