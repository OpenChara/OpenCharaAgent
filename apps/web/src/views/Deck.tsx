/* Deck — the card list/grid + card-view editor + 2-step wake, faithful to
 * index.html #view-deck + app.js renderDeck (1081) / viewCard (1146) /
 * openWakeSheet (1806). The grid splits unwoken (your editable OCs/drafts) from
 * woken (living charas' locked cards) via the filter seg; spines carry
 * copy/view/wake actions (locked cards: wake+copy only).
 *
 * Binding UI rule: the filter seg flips immediately (optimistic); copy/wake show
 * a working state and surface errors. The wake button gates on a configured model
 * (ensureModel) — when there's no key it routes to settings rather than failing
 * silently. Import / builtin-picker / create-flow open via the overlay layer
 * (state/overlay.tsx); new-card + builtin gate through ensureModel like wake. */

import { useMemo, useState } from "react";
import { useT } from "../i18n";
import { useHub } from "../state/hub";
import { useEnsureModel } from "../components/overlays/useEnsureModel";
import { useOverlay } from "../state/overlay";
import { paletteClass } from "../lib/format";
import { rpcErrText } from "../lib/status";
import { CardFace } from "../components/deck/visual";
import { BrandLoader } from "../components/ui/BrandLoader";
import { CardEditor } from "../components/deck/CardEditor";
import { WakeSheet } from "../components/deck/WakeSheet";
import { deckToast } from "../components/ui/deckToast";
import type { DeckCard } from "../components/deck/types";

type Filter = "unwoken" | "woken";

export function Deck() {
  const t = useT();
  const overlay = useOverlay();
  const { hub, snapshot, refresh } = useHub();
  const [filter, setFilter] = useState<Filter>("unwoken");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<DeckCard | null>(null);
  const [waking, setWaking] = useState<DeckCard | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const allCards = useMemo(
    () => (snapshot?.cards as DeckCard[] | undefined) || [],
    [snapshot?.cards],
  );

  const cards = useMemo(() => {
    const q = query.toLowerCase();
    return allCards.filter((c) => {
      const matchesFilter = filter === "woken" ? !!c.locked : !c.locked && !c.builtin;
      return matchesFilter && (!q || c.name.toLowerCase().includes(q));
    });
  }, [allCards, filter, query]);

  const setBusyKey = (key: string, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });

  // A wake/create needs a configured model; the shared hook opens the in-flow
  // ModelGate (and RESUMES the action on key save) instead of ejecting to Settings.
  const ensureModel = useEnsureModel();

  const duplicate = async (c: DeckCard) => {
    setBusyKey("dup:" + c.path, true);
    try {
      // Backend-owned: copies the card.json AND its art-asset sidecars into a new
      // folder, renames with a 副本/copy suffix, and lifts PNG cards to JSON.
      await hub.call("card.duplicate", { path: c.path }, 20000);
      deckToast(t("copied"));
      await refresh();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setBusyKey("dup:" + c.path, false);
    }
  };

  const total = cards.length;

  return (
    <div className="view active" id="view-deck">
      <div className="toolbar">
        <h1>
          <span>{t("nav-deck")}</span>
          <span className="count">{total ? `· ${total}` : ""}</span>
        </h1>
        <div className="grow" />
        <input
          className="searchfield"
          placeholder={t("search")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="seg deck-filterseg">
          {(["unwoken", "woken"] as Filter[]).map((m) => (
            <span key={m} className={filter === m ? "on" : ""} onClick={() => setFilter(m)}>
              {t(m === "unwoken" ? "deck-unwoken" : "deck-woken")}
            </span>
          ))}
        </div>
        {/* ✨默认 builtin carousel + ＋新角色卡 create-flow (§6 overlays). Both route
            through ensureModel like wake does. Card import is deferred — to start from
            a card you have elsewhere (e.g. SillyTavern), paste its JSON into the create box. */}
        <button className="btn pick-default" onClick={() => overlay.open({ kind: "builtin" })}>
          {t("deck-pick")}
        </button>
        <button className="btn primary" onClick={() => ensureModel(() => overlay.open({ kind: "create" }))}>
          {t("new-card")}
        </button>
      </div>

      <div className="deck">
        {/* snapshot null = the first roster load hasn't returned yet ("loading"),
            distinct from "loaded, no cards match the filter" — show the loader
            for the former so the deck doesn't flash an empty-state on cold open. */}
        {!snapshot ? (
          <BrandLoader />
        ) : !total ? (
          <div className="empty-state deck-empty">{t(filter === "woken" ? "deck-empty-woken" : "deck-empty-unwoken")}</div>
        ) : (
          <div className="deck-grid">
            {cards.map((c) => {
              const sub =
                c.locked && c.owner
                  ? t("deck-owned", { name: c.owner })
                  : [c.tagline || c.world, c.builtin ? t("deck-builtin") : "", ...(c.tags || [])]
                      .filter(Boolean)
                      .slice(0, 3)
                      .join(" · ");
              const dupBusy = busy.has("dup:" + c.path);
              return (
                <div
                  key={c.path}
                  className={"spine" + (c.locked ? " locked" : "")}
                  onClick={() => setEditing(c)}
                >
                  <CardFace card={c} cls={`face ${paletteClass(c.name)}`}>
                    {c.locked ? (
                      <div className="lock-badge">{c.owner ? t("deck-owned", { name: c.owner }) : t("deck-readonly")}</div>
                    ) : c.draft ? (
                      <div className="draft-badge">{t("deck-draft")}</div>
                    ) : null}
                    <div className="spine-acts">
                      <button
                        className="wake"
                        onClick={(ev) => {
                          ev.stopPropagation();
                          ensureModel(() => setWaking(c));
                        }}
                      >
                        {t("deck-wake")}
                      </button>
                      {!c.locked && (
                        <button
                          onClick={(ev) => {
                            ev.stopPropagation();
                            setEditing(c);
                          }}
                        >
                          {t("deck-view")}
                        </button>
                      )}
                      <button
                        disabled={dupBusy}
                        onClick={(ev) => {
                          ev.stopPropagation();
                          void duplicate(c);
                        }}
                      >
                        {dupBusy ? <span className="spin" /> : t("deck-copy")}
                      </button>
                    </div>
                  </CardFace>
                  <div className="sbody">
                    <div className="sname">
                      <b>{c.name}</b>
                      <span className="chip">{c.lang}</span>
                    </div>
                    <div className="sworld">{sub}</div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {editing && (
        <CardEditor
          key={editing.path}
          card={editing}
          onClose={() => setEditing(null)}
          onChanged={() => void refresh()}
          onWake={(c) => ensureModel(() => setWaking(c))}
        />
      )}
      {waking && <WakeSheet card={waking} onClose={() => setWaking(null)} />}
    </div>
  );
}
