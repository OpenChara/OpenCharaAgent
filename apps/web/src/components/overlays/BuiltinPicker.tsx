/* BuiltinPicker — the ✨默认 recommended-character carousel, a React port of
 * builtins.js buildBuiltinPicker + builtinTile + selectBuiltin. Two swipeable
 * pages of four portrait tiles; selecting one routes through the model gate then
 * opens the 2-step wake sheet (the exact deck "Wake" path).
 *
 * The body is reusable: the first-run overlay embeds it, and the deck opens it
 * standalone in a modal-style overlay. On touch (no hover) the first tap reveals a
 * tile's copy and the second selects (builtins.js isTouch logic). */

import { useRef, useState } from "react";
import { assetUrl } from "../../rpc";
import { useT, useLang } from "../../i18n";
import { useHub } from "../../state/hub";
import { useOverlay } from "../../state/overlay";
import { glyphOf, paletteClass } from "../../lib/format";
import { avatarSrc, themeStyle } from "../deck/visual";
import { DeckModal } from "../ui/DeckModal";
import { useEnsureModel } from "./useEnsureModel";
import { BUILTIN_COPY, BUILTIN_PAGES, builtinCard } from "./builtins";
import type { DeckCard } from "../deck/types";

const IS_TOUCH = typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(hover: none)").matches;

function BuiltinTile({ name, card, onSelect }: { name: string; card: DeckCard; onSelect: (c: DeckCard) => void }) {
  const { lang } = useLang();
  const [open, setOpen] = useState(false);
  const copy = BUILTIN_COPY[name] || { zh: "", en: "", tags: [] };
  const theme = themeStyle(card);
  const avSrc = avatarSrc(card);
  const bgSrc = card.sprite_url || card.keyvisual_url || card.bg_url || "";

  const select = () => {
    if (IS_TOUCH && !open) {
      setOpen(true);
      return;
    }
    onSelect(card);
  };

  return (
    <div
      className={"bp-cell" + (open ? " bp-open" : "")}
      tabIndex={0}
      role="button"
      aria-label={card.name}
      style={theme}
      onClick={select}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(card);
        }
      }}
    >
      <div className={"bp-avatar" + (avSrc ? "" : " " + paletteClass(card.name))} style={theme}>
        {avSrc ? <img src={avSrc} alt="" /> : glyphOf(card.name)}
      </div>
      <div className={"bp-tile" + (bgSrc ? "" : " bp-flat")}>
        {bgSrc && <div className="bp-bg" style={{ backgroundImage: `url("${assetUrl(bgSrc)}")` }} />}
        <div className="bp-scrim" />
        <div className="bp-name">{card.name}</div>
        <div className="bp-reveal">
          <div className="bp-desc">{lang === "en" ? copy.en : copy.zh}</div>
          <div className="bp-tags">
            {(copy.tags || []).map((tag, i) => (
              <span className="bp-chip" key={i}>{lang === "en" ? tag.en : tag.zh}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

/** The carousel body — pages, arrows, dots, swipe. Reusable inside any host. */
export function BuiltinPickerBody({ onSelect }: { onSelect: (card: DeckCard) => void }) {
  const t = useT();
  const { snapshot } = useHub();
  const cards = (snapshot?.cards as DeckCard[] | undefined) || [];
  const [page, setPage] = useState(0);
  const sx = useRef({ x: 0, y: 0 });

  const pages = BUILTIN_PAGES.length;
  const goTo = (p: number) => setPage(Math.max(0, Math.min(pages - 1, p)));

  return (
    <div className="bp-root">
      <div className="bp-head">{t("bp-header")}</div>
      <div
        className="bp-viewport"
        onTouchStart={(e) => {
          sx.current = { x: e.touches[0].clientX, y: e.touches[0].clientY };
        }}
        onTouchEnd={(e) => {
          // Horizontal swipe past a threshold flips the page (builtins.js).
          const dx = e.changedTouches[0].clientX - sx.current.x;
          const dy = e.changedTouches[0].clientY - sx.current.y;
          if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy)) goTo(page + (dx < 0 ? 1 : -1));
        }}
      >
        <div className="bp-track" style={{ transform: `translateX(${-page * 100}%)` }}>
          {BUILTIN_PAGES.map((pageNames, pi) => (
            <div className="bp-grid" key={pi}>
              {pageNames.map((nm) => {
                const card = builtinCard(cards, nm);
                return card ? <BuiltinTile key={nm} name={nm} card={card} onSelect={onSelect} /> : null;
              })}
            </div>
          ))}
        </div>
        <button
          className={"bp-arrow prev" + (page === 0 ? " hidden" : "")}
          aria-label="previous"
          onClick={() => goTo(page - 1)}
        >
          ‹
        </button>
        <button
          className={"bp-arrow next" + (page === pages - 1 ? " hidden" : "")}
          aria-label="next"
          onClick={() => goTo(page + 1)}
        >
          ›
        </button>
      </div>
      <div className="bp-dots">
        {Array.from({ length: pages }, (_, i) => (
          <i key={i} className={i === page ? "on" : ""} onClick={() => goTo(i)} />
        ))}
      </div>
    </div>
  );
}

/** The standalone picker overlay (reopened from the card deck). */
export function BuiltinPicker({ onClose }: { onClose: () => void }) {
  const ensureModel = useEnsureModel();
  const overlay = useOverlay();

  // Route a picked card into the wake flow (builtins.js selectBuiltin): dismiss
  // the picker first so the model gate / wake sheet never sits behind it.
  const onSelect = (card: DeckCard) => {
    onClose();
    ensureModel(() => overlay.open({ kind: "wake", card }));
  };

  return (
    <DeckModal open variant="wide" onClose={onClose}>
      <BuiltinPickerBody onSelect={onSelect} />
    </DeckModal>
  );
}
