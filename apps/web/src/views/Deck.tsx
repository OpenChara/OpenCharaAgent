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
import { useOverlay } from "../state/overlay";
import { useNavigate } from "../hooks/useHashRoute";
import { paletteClass } from "../lib/format";
import { rpcErrText } from "../lib/status";
import { CardFace } from "../components/deck/visual";
import { CardEditor } from "../components/deck/CardEditor";
import { WakeSheet } from "../components/deck/WakeSheet";
import { deckToast } from "../components/ui/deckToast";
import type { DeckCard, FullCard } from "../components/deck/types";

type Filter = "unwoken" | "woken";

export function Deck() {
  const t = useT();
  const nav = useNavigate();
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
  const defaults = (snapshot?.defaults as { has_key?: boolean; base_url?: string }) || {};

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

  // A wake needs a configured model; without a key, route to settings (the SPA's
  // stand-in for app.js's ensureModel → first-run model setup overlay).
  const ensureModel = (action: () => void) => {
    if (defaults.has_key && defaults.base_url) action();
    else {
      deckToast(t("go-settings"));
      nav("#/settings");
    }
  };

  const duplicate = async (c: DeckCard) => {
    setBusyKey("dup:" + c.path, true);
    try {
      const full = await hub.call<FullCard>("card.read", { path: c.path }, 20000);
      if (!full.raw) throw new Error(t("dup-png"));
      const zh = String(full.language || c.lang || "").toLowerCase().startsWith("zh");
      const taken = new Set(allCards.map((x) => x.name));
      const base = `${full.name || c.name} ${zh ? "副本" : "copy"}`;
      let name = base;
      for (let n = 2; taken.has(name); n++) name = `${base} ${n}`;
      if (full.raw.data) (full.raw.data as { name?: string }).name = name;
      full.raw.name = name;
      await hub.call("card.save", { data: full.raw }, 20000);
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
        {/* ✨默认 builtin carousel + 导入 import + ＋新角色卡 create-flow (§6 overlays).
            New-card + builtin route through ensureModel like wake does; import does not. */}
        <button className="btn pick-default" onClick={() => overlay.open({ kind: "builtin" })}>
          {t("deck-pick")}
        </button>
        <button className="btn soft" onClick={() => overlay.open({ kind: "import" })}>
          {t("import")}
        </button>
        <button className="btn primary" onClick={() => ensureModel(() => overlay.open({ kind: "create" }))}>
          {t("new-card")}
        </button>
      </div>

      <div className="deck">
        <div className="deck-grid">
          {!total ? (
            <div className="deck-empty">{t(filter === "woken" ? "deck-empty-woken" : "deck-empty-unwoken")}</div>
          ) : (
            cards.map((c) => {
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
            })
          )}
        </div>
      </div>

      {editing && (
        <CardEditor
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
