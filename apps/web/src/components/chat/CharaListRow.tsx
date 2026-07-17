/* One conversation-list row (WeChat-style): avatar · name · relative time ·
 * one-line muted preview. Presentation-only — the caller derives every string
 * (preview via statusOf/last_message, time via timeAgo) and handles navigation,
 * so the same row can serve the mobile characters home today and a desktop
 * chat-view left column later. The autonomy dot reuses the board's `.dot`
 * live|off|err classes on the avatar corner. */

import { Avatar } from "../deck/visual";
import type { DeckCard } from "../deck/types";

export interface CharaListRowProps {
  /** Display name (the card's char name). */
  charName: string;
  /** The chara's frozen deck card (avatar art / theme); null → palette+glyph. */
  card?: DeckCard | null;
  /** Autonomy dot — the same live|off|err state the board card shows. */
  dot: "live" | "off" | "err";
  /** The one-line preview (last message, "你: …"-prefixed, or the life-state line). */
  preview: string;
  /** Render the preview in the error color. */
  previewErr?: boolean;
  /** Right-aligned relative time label ("" hides it). */
  time: string;
  /** Row tap/Enter — the caller navigates. */
  onOpen: () => void;
  /** Highlight as the open conversation (desktop left-column reuse). */
  active?: boolean;
}

export function CharaListRow({
  charName,
  card,
  dot,
  preview,
  previewErr,
  time,
  onOpen,
  active,
}: CharaListRowProps) {
  return (
    <div
      className={`crow${active ? " active" : ""}`}
      role="button"
      tabIndex={0}
      aria-label={charName}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="crow-ava">
        <Avatar name={charName} card={card} />
        <span className={`dot ${dot}`} />
      </div>
      <div className="crow-main">
        <div className="crow-top">
          <b className="crow-name">{charName}</b>
          {time && <span className="crow-time">{time}</span>}
        </div>
        <div className={`crow-preview${previewErr ? " err" : ""}`}>{preview}</div>
      </div>
    </div>
  );
}
