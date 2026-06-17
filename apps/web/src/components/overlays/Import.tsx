/* Import — the card-file import overlay (app.js importCardFile + the hidden
 * file-input + document drag-drop, 1420-1448). A small modal with a drop zone +
 * a file picker; on success it toasts, refreshes the deck, and routes there.
 *
 * Binding UI rule: the upload shows a working state and surfaces errors; the
 * drop zone reacts to drag-over immediately. Copy reuses existing i18n keys
 * (btn-import / import / imported) — no new strings. */

import { useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { DeckModal } from "../ui/DeckModal";
import { deckToast } from "../ui/deckToast";
import { importCardFile, isCardFile } from "./importCard";

export function Import({ onClose }: { onClose: () => void }) {
  const t = useT();
  const { refresh } = useHubApi();
  const nav = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);

  const upload = async (file: File) => {
    if (!isCardFile(file.name)) {
      deckToast(t("btn-import"), true);
      return;
    }
    setBusy(true);
    try {
      await importCardFile(file);
      deckToast(t("imported", { name: file.name }));
      await refresh();
      onClose();
      nav("#/deck");
    } catch (e) {
      setBusy(false);
      deckToast((e as { message?: string })?.message || t("btn-import"), true);
    }
  };

  return (
    <DeckModal open onClose={onClose}>
      <div>
        <h2>{t("import")}</h2>
        <div className="sub">{t("btn-import")}</div>
        <div
          className={"import-drop" + (dragOver ? " over" : "")}
          onClick={() => !busy && fileRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            const f = e.dataTransfer.files?.[0];
            if (f) void upload(f);
          }}
          style={{
            margin: "16px 0",
            padding: "32px 18px",
            border: "1.5px dashed var(--line, rgba(128,128,128,.4))",
            borderRadius: 12,
            textAlign: "center",
            cursor: busy ? "default" : "pointer",
            opacity: dragOver ? 0.85 : 1,
          }}
        >
          {busy ? <span className="spin" /> : <div className="btn primary">{t("import")}</div>}
          <div className="sub" style={{ marginTop: 10 }}>SillyTavern PNG / JSON</div>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept=".json,.png"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void upload(f);
          }}
        />
        <div className="acts">
          <button className="btn text" onClick={onClose}>{t("cancel")}</button>
        </div>
      </div>
    </DeckModal>
  );
}
