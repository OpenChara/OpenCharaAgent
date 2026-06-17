/* The composer — textarea + attach + send/stop, ported from chat.js bindUI/submit.
 *
 * Optimistic UI (CLAUDE.md binding): the send button flips to ■ the instant a
 * turn is in flight; an empty box + ■ click interrupts; a slash command runs as a
 * control line; busy + text stages the message (the hook queues it). Attachments
 * stage as chips and are read to raw base64 on pick.
 */

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useT } from "../../i18n";
import { errMsg } from "../../lib/status";
import type { StagedAttachment } from "../../hooks/useCharaStream";
import { readAttachment, humanSize, ATTACH_MAX_BYTES, ATTACH_ACCEPT_ALL } from "./attachments";

export function Composer({
  charName,
  streaming,
  resting,
  statusSlot,
  onSend,
  onInterrupt,
  onCommand,
  onError,
}: {
  charName: string;
  streaming: boolean;
  resting: boolean;
  /** the work-status line, rendered at the top of the composer-wrap (chat.js). */
  statusSlot?: ReactNode;
  onSend: (text: string, atts: StagedAttachment[]) => void;
  onInterrupt: () => void;
  onCommand: (line: string) => void;
  onError: (msg: string) => void;
}) {
  const t = useT();
  // Persist the unsent text draft per-chara so leaving the chat (back, a tab, a
  // sidebar click) doesn't silently throw away a half-typed message — it's restored
  // on return. (Attachments are blobs and aren't persisted; only the text.)
  const draftKey = (n: string) => `lm-composer-draft:${n}`;
  const readDraft = (n: string): string => {
    try {
      return localStorage.getItem(draftKey(n)) || "";
    } catch {
      return "";
    }
  };
  const [text, setText] = useState(() => readDraft(charName));
  const [staged, setStaged] = useState<StagedAttachment[]>([]);
  // Reload when switching chara (same Composer instance, new charName).
  useEffect(() => {
    setText(readDraft(charName));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [charName]);
  // Write-through on every change; clears the key when the draft is empty (e.g. sent).
  useEffect(() => {
    try {
      if (text) localStorage.setItem(draftKey(charName), text);
      else localStorage.removeItem(draftKey(charName));
    } catch {
      /* private mode — ignore */
    }
  }, [text, charName]);
  const [stopping, setStopping] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const stageFiles = async (list: FileList | File[] | null) => {
    const files = Array.from(list || []);
    for (const f of files) {
      if (!f) continue;
      if (f.size > ATTACH_MAX_BYTES) {
        onError(t("attach-too-big", { name: f.name || t("attach-file") }));
        continue;
      }
      try {
        const att = await readAttachment(f);
        setStaged((prev) => [...prev, att]);
      } catch (e) {
        onError(errMsg(e));
      }
    }
  };

  const unstage = (att: StagedAttachment) => setStaged((prev) => prev.filter((a) => a !== att));

  const submit = () => {
    const trimmed = text.trim();
    const hasAttach = staged.length > 0;
    if (!trimmed && !hasAttach) return;
    // A slash command is a control line — never carries attachments.
    if (trimmed.startsWith("/") && !hasAttach) {
      if (streaming) {
        onError(t("busy-cmd"));
        return;
      }
      setText("");
      onCommand(trimmed);
      return;
    }
    const atts = staged;
    setText("");
    setStaged([]);
    onSend(trimmed, atts); // the hook queues if busy
  };

  const onSendClick = () => {
    const hasText = text.trim().length > 0;
    if (streaming && !hasText) {
      setStopping(true); // optimistic: interrupt landed
      onInterrupt();
    } else {
      submit();
    }
  };

  const onKeyDown = (ev: KeyboardEvent<HTMLTextAreaElement>) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.nativeEvent.isComposing) {
      ev.preventDefault();
      submit();
    }
  };

  const showStop = streaming && text.trim().length === 0;

  return (
    <div className="composer-wrap">
      {statusSlot}
      {staged.length > 0 && (
        <div className="attach-stage">
          {staged.map((att, i) =>
            att.isImage ? (
              <div key={i} className="attach-chip" title={att.name}>
                <img className="thumb" src={att.url} alt={att.name} />
                <button className="rm" title={t("attach-remove")} onClick={() => unstage(att)}>
                  ×
                </button>
              </div>
            ) : (
              <div key={i} className="attach-chip file" title={att.name}>
                <span className="ficon">📄</span>
                <div className="meta">
                  <span className="fname">{att.name}</span>
                  <span className="fsize">{humanSize(att.size)}</span>
                </div>
                <button className="rm" title={t("attach-remove")} onClick={() => unstage(att)}>
                  ×
                </button>
              </div>
            ),
          )}
        </div>
      )}
      <div className="composer">
        <input
          ref={fileRef}
          type="file"
          multiple
          accept={ATTACH_ACCEPT_ALL}
          hidden
          onChange={(e) => {
            void stageFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <button
          className="attach-btn"
          title={t("attach-add")}
          aria-label={t("attach-add")}
          onClick={() => fileRef.current?.click()}
        >
          +
        </button>
        <textarea
          ref={taRef}
          rows={1}
          value={text}
          placeholder={resting ? t("composer-resting-ph") : t("composer-ph", { name: charName })}
          onChange={(e) => {
            setText(e.target.value);
            const el = e.target;
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 130) + "px";
          }}
          onKeyDown={onKeyDown}
        />
        <button
          className={showStop ? `stop${stopping ? " stopping" : ""}` : "send"}
          disabled={stopping && showStop}
          onClick={onSendClick}
        >
          {showStop ? "■" : "↑"}
        </button>
      </div>
    </div>
  );
}
