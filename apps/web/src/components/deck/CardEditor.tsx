/* CardEditor — the card view = card editor, a React port of app.js:1146 viewCard.
 * A user JSON template edits in place (every section + Advanced presence markers
 * + raw JSON); builtins/locked cards are read-only (copy/wake to change). Tabs:
 * 设定 (set) / 视觉 (vis) / 表情 (emo) / 世界 (world).
 *
 * Save folds the edited fields back into the raw card (card.save) — the pure
 * serialization lives in lib/cards (serializeCardFields); the field reads come off
 * the uncontrolled CardField handles. Binding UI rule: save/delete/copy show a
 * working state and surface errors. */

import { useEffect, useRef, useState } from "react";
import { assetUrl } from "../../rpc";
import { useT, type TKey } from "../../i18n";
import { useHubApi, useHubState } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { glyphOf, paletteClass, fmtSize } from "../../lib/format";
import { fileToB64 } from "../../lib/file";
import { sectionText, serializeCardFields, type NormalizedDraft, type CardData } from "../../lib/cards";
import { CardField, CardBlock, cardCtxString, type FieldHandle } from "./CardField";
import { Avatar, avatarSrc, themeOf, themeStyle } from "./visual";
import { VisualEditor } from "./VisualEditor";
import { deckToast, deckToastAction } from "../ui/deckToast";
import { DeckModal } from "../ui/DeckModal";
import type { DeckCard, FullCard, CardExtLunamoth, WorldBookEntry } from "./types";

type Tab = "set" | "vis" | "emo" | "assets" | "world";

export function CardEditor({
  card,
  onClose,
  onChanged,
  onWake,
}: {
  card: DeckCard;
  onClose: () => void;
  onChanged: () => void;
  onWake: (c: DeckCard) => void;
}) {
  const t = useT();
  const { hub, refresh } = useHubApi();
  const { snapshot } = useHubState();
  const [full, setFull] = useState<FullCard | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("set");
  const [dupBusy, setDupBusy] = useState(false);
  const [genRunning, setGenRunning] = useState(false);

  // A living chara's OWN frozen card (locked + owner) is edited LIVE: persistence is
  // immediate (card.patch field-level + chara.set_aspiration), activation is per
  // prompt-zone (soul → next start via 立即应用; aspiration → next turn).
  const isJsonCard = card.path.toLowerCase().endsWith(".json");
  const liveCard = !!card.locked && !!card.owner && isJsonCard;
  const liveSession = liveCard ? (snapshot?.sessions || []).find((s) => s.name === card.owner) : undefined;

  const fName = useRef<FieldHandle>(null);
  const fTagline = useRef<FieldHandle>(null);
  const fDesc = useRef<FieldHandle>(null);
  const fPers = useRef<FieldHandle>(null);
  const fScen = useRef<FieldHandle>(null);
  const fFirst = useRef<FieldHandle>(null);
  const fGoals = useRef<FieldHandle>(null);
  const fNotes = useRef<FieldHandle>(null);
  const fWorld = useRef<FieldHandle>(null);
  const liveSaveTimer = useRef<number | null>(null);
  const lastAspiration = useRef<string | null>(null);
  const baseline = useRef<Record<string, string>>({});  // last-saved soul values
  const [applying, setApplying] = useState(false);

  // Cross-tab staging: CardField is uncontrolled (text lives in the DOM), and the
  // 设定/世界 panes unmount on tab switch — so without this, edits on a tab you leave
  // are lost. `staged` holds each field's text once it's been flushed (on tab switch
  // and at save); a remounted pane seeds from it, and Save reads from it, so edits
  // across all tabs survive until you click 保存.
  const [staged, setStaged] = useState<Record<string, string>>({});
  const fieldRefs: Record<string, { current: FieldHandle | null }> = {
    name: fName, tagline: fTagline, description: fDesc, personality: fPers,
    scenario: fScen, first_mes: fFirst, goals: fGoals, creator_notes: fNotes, world: fWorld,
  };
  const flushFields = () =>
    setStaged((prev) => {
      const next = { ...prev };
      for (const k in fieldRefs) {
        const v = fieldRefs[k].current?.value();
        if (v !== undefined) next[k] = v;
      }
      return next;
    });
  // Flush the mounted tab's edits into `staged` BEFORE switching, so they aren't lost.
  const switchTab = (k: Tab) => {
    flushFields();
    setTab(k);
  };
  // The value to seed a field with: a staged edit wins over the card's saved value.
  const seed = (k: string, fallback: string) => (staged[k] !== undefined ? staged[k] : fallback);

  useEffect(() => {
    let alive = true;
    hub
      .call<FullCard>("card.read", { path: card.path }, 20000)
      .then((f) => alive && setFull(f))
      .catch((e) => alive && setErr(rpcErrText(t, e as { message?: string })));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.path]);

  // Drop any pending debounced live-save when the editor unmounts, so a 600ms timer
  // armed by a field blur on close can't fire a stray card.patch after teardown.
  useEffect(() => () => {
    if (liveSaveTimer.current) window.clearTimeout(liveSaveTimer.current);
  }, []);

  // Poll whether a visual is still generating for this card → the wake button shows
  // 生成中 and warns (waking mid-generation would freeze a card missing the image). Only
  // for generatable cards (a builtin / PNG can't generate, so don't poll the hub for it).
  useEffect(() => {
    if (card.builtin || !isJsonCard) return;
    let alive = true;
    const tick = () =>
      hub
        .call<{ running?: number }>("card.visual_jobs", { path: card.path }, 10000)
        .then((r) => { if (alive) setGenRunning((r?.running || 0) > 0); })
        .catch(() => {});
    void tick();
    const id = window.setInterval(tick, 2500);
    return () => { alive = false; window.clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.path]);

  if (err) {
    return (
      <DeckModal open variant="wide" onClose={onClose}>
        <div className="cv-note">{err}</div>
        <div className="acts" style={{ marginTop: 14 }}>
          <button className="btn text" onClick={onClose}>{t("cancel")}</button>
        </div>
      </DeckModal>
    );
  }
  if (!full) {
    return (
      <DeckModal open variant="wide" onClose={onClose}>
        <div className="wake-loading"><span className="spin" /> {t("thinking-live")}</div>
      </DeckModal>
    );
  }

  const ext = (full.extensions && full.extensions.lunamoth ? full.extensions.lunamoth : {}) as CardExtLunamoth;
  const isJson = !!full.raw;
  // A deck template edits in place; a living chara's own card edits LIVE (locked but
  // owned). Both are editable; only builtin / PNG / a non-owned locked card stay read-only.
  const editable = !card.builtin && isJson && (!card.locked || liveCard);
  // Visuals are editable on LOCKED cards too — a living chara owns a frozen card,
  // and its art (立绘/背景/头像/keyvisual) is set through the same save_asset RPCs,
  // which the backend allows on a locked card. Only the soul/world stay read-only
  // when locked; builtin + PNG cards remain fully read-only.
  const visualEditable = !card.builtin && isJson;
  const charName = full.name || card.name;
  const taglineValue = String(ext.tagline || card.tagline || "");
  const book =
    full.character_book && Array.isArray(full.character_book.entries) ? full.character_book : null;
  const worldText = sectionText(
    {
      world_entries: (book ? book.entries! : []).map((e2) => ({
        keys: e2.keys || [],
        content: e2.content || "",
        constant: !!e2.constant,
      })),
    } as NormalizedDraft,
    "world_entries",
  );
  // Polaris: a single north-star string (was the old wishes/goals list).
  const goalsText = typeof ext.polaris === "string" ? ext.polaris : "";

  const editorCtx = () =>
    cardCtxString({
      name: charName,
      description: fDesc.current?.value(),
      personality: fPers.current?.value(),
      scenario: fScen.current?.value(),
      tagline: fTagline.current?.value(),
    });

  const note: TKey | "" = card.builtin
    ? "cv-builtin-note"
    : !isJson
      ? "cv-png-note"
      : card.frozen
        ? "av-frozen-note"
        : "";

  // ── field-level AUTO-SAVE (no whole-card replace, no Save button) ──────────────
  // Every editable card (deck template OR a living chara's own card) saves on blur /
  // tab-switch / close via card.patch — so a partial submit can never blank the card
  // and there's nothing "unsaved" to discard. A LIVING chara's soul → card.patch (next
  // start, flags 待应用); its aspiration → chara.set_aspiration (next turn). A deck
  // card's aspiration is just a card field, folded into the same patch. We only patch
  // when a field actually changed, so an aspiration-only edit never shows 待应用.
  const SOUL_KEYS = ["name", "description", "personality", "scenario", "first_mes", "creator_notes", "tagline", "world"];
  const origVal = (k: string): string =>
    k === "name" ? full?.name || ""
    : k === "tagline" ? taglineValue
    : k === "world" ? worldText
    : k === "goals" ? goalsText
    : String((full as Record<string, unknown> | null)?.[k] ?? "");
  const base = (k: string) => baseline.current[k] ?? origVal(k);  // moving last-saved value
  const aspLive = liveCard;  // live chara: aspiration goes through set_aspiration (next turn)
  const patchKeys = aspLive ? SOUL_KEYS : [...SOUL_KEYS, "goals"];
  const liveSave = async () => {
    if (!editable || !full?.raw) return;
    flushFields();
    const val = (k: string) => fieldRefs[k].current?.value() ?? staged[k];
    if (lastAspiration.current === null) lastAspiration.current = goalsText;
    try {
      const changed = patchKeys.some((k) => (val(k) ?? base(k)) !== base(k));
      if (changed) {
        const data: CardData = {};
        serializeCardFields(
          data,
          {
            name: val("name"), description: val("description"), personality: val("personality"),
            scenario: val("scenario"), first_mes: val("first_mes"), creator_notes: val("creator_notes"),
            tagline: val("tagline"), world: val("world"),
            // a deck card folds aspiration into the patch; a live chara handles it below
            goals: aspLive ? undefined : val("goals"),
          },
          (full.raw.name as string) || full.name || "",
        );
        await hub.call("card.patch", { path: card.path, fields: data }, 20000);
        // advance the baseline so a later auto-save doesn't re-patch (and re-mark a live
        // chara dirty) the same content — especially the trailing save after 立即应用.
        for (const k of patchKeys) baseline.current[k] = val(k) ?? base(k);
      }
      if (aspLive) {
        const asp = val("goals");
        if (asp !== undefined && asp !== lastAspiration.current) {
          lastAspiration.current = asp;
          await hub.call("chara.set_aspiration", { name: card.owner, text: asp }, 15000);
        }
      }
      await refresh();
      onChanged();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };
  const liveSaveDebounced = () => {
    if (liveSaveTimer.current) window.clearTimeout(liveSaveTimer.current);
    liveSaveTimer.current = window.setTimeout(() => void liveSave(), 600);
  };
  // Patch one extension field immediately (used by the editable theme-color pickers).
  const patchExt = async (patch: Record<string, unknown>) => {
    try {
      await hub.call("card.patch", { path: card.path, fields: { extensions: { lunamoth: patch } } }, 20000);
      await refresh();
      onChanged();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };
  const doApply = async () => {
    setApplying(true);
    if (liveSaveTimer.current) window.clearTimeout(liveSaveTimer.current);  // no trailing re-save
    try {
      await liveSave(); // flush any pending edit first
      await hub.call("chara.apply_card", { name: card.owner }, 60000);
      deckToast(t("cv-applied"));
      await refresh();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setApplying(false);
    }
  };
  const closeEditor = () => {
    if (liveSaveTimer.current) window.clearTimeout(liveSaveTimer.current);  // no trailing fire after unmount
    if (editable) void liveSave().finally(onClose);
    else onClose();
  };

  const doDelete = async () => {
    if (!confirm(t("deck-delete-q", { name: charName }))) return;
    try {
      // Soft delete → the card moves to trash and is restorable; offer an Undo.
      const r = await hub.call<{ trash_id?: string }>("card.delete", { path: card.path }, 10000);
      onClose();
      onChanged();
      if (r.trash_id) {
        deckToastAction(t("card-deleted", { name: charName }), t("undo"), () => {
          void hub
            .call("card.restore", { trash_id: r.trash_id }, 10000)
            .then(() => {
              onChanged();
              deckToast(t("restored"));
            })
            .catch((e) => deckToast(rpcErrText(t, e as { message?: string }), true));
        });
      }
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };

  const doDuplicate = async () => {
    setDupBusy(true);
    try {
      const f = await hub.call<FullCard>("card.read", { path: card.path }, 20000);
      if (!f.raw) throw new Error(t("dup-png"));
      const zh = String(f.language || card.lang || "").toLowerCase().startsWith("zh");
      const baseName = `${f.name || card.name} ${zh ? "副本" : "copy"}`;
      if (f.raw.data) (f.raw.data as { name?: string }).name = baseName;
      f.raw.name = baseName;
      await hub.call("card.save", { data: f.raw }, 20000);
      deckToast(t("copied"));
      onClose();
      onChanged();
    } catch (e) {
      setDupBusy(false);
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };

  const cardForVisual: DeckCard = card;
  const th = themeOf(card);
  const hasAnyArt = !!(card.sprite_url || card.keyvisual_url || card.bg_url || avatarSrc(card));
  // Clicking the avatar jumps to the 视觉 tab, where the R9 VisualEditor owns the
  // whole visual set (generate / upload / replace / delete per kind). The old
  // SVG-gen + dual-theme AvatarEditor overlay was retired — VisualEditor replaces it.
  const goVisual = () => switchTab("vis");

  // Activation badges for live editing: soul fields ride the cache-stable prefix
  // (next start), the aspiration rides the per-turn volatile tail (next turn).
  const soulBadge = liveCard ? <span className="cv-zone-badge">{t("cv-zone-next-start")}</span> : undefined;
  const aspBadge = liveCard ? <span className="cv-zone-badge turn">{t("cv-zone-next-turn")}</span> : undefined;

  return (
    <DeckModal open variant="cardview" onClose={closeEditor} style={themeStyle(card)}>
      <div
        className="cardview"
        onBlurCapture={editable ? liveSaveDebounced : undefined}
      >
        {note && (
          <div className="cv-note cv-note-top">
            {note === "av-frozen-note" ? t(note, { names: (card.used_by || []).join("、") }) : t(note)}
          </div>
        )}
        {liveCard && (
          <div className="cv-live-note">{t("cv-live-edit-note", { name: card.owner || charName })}</div>
        )}
        {liveCard && liveSession?.card_dirty && (
          <div className="cv-apply-banner">
            <span>{t("cv-apply-pending")}</span>
            <button className="btn primary sm" disabled={applying} onClick={() => void doApply()}>
              {applying ? <span className="spin" /> : t("cv-apply-now")}
            </button>
          </div>
        )}
        <div className="cv-header">
          <Avatar name={charName} card={card} cls="avatar-s" onClick={goVisual} title={t("cv-tab-vis")} />
          <div className="cv-id">
            <CardField ref={fName} editable={editable} initial={seed("name", full.name || "")} className="cve-name" />
            {(editable || taglineValue) && (
              <CardField ref={fTagline} editable={editable} initial={seed("tagline", taglineValue)} placeholder={t("sec-tagline")} className="tagline" />
            )}
            <div className="cv-badges">
              <span className="chip">{full.language || card.lang}</span>
              {card.builtin && <span className="chip">{t("deck-builtin")}</span>}
              {(ext.force_roleplay === true || ext.embodiment === "actor") && (
                <span className="chip">{t("mod-roleplay")}</span>
              )}
              {card.frozen &&<span className="chip">{t("card-frozen-by", { names: (card.used_by || []).join("、") })}</span>}
            </div>
          </div>
        </div>

        <div className="cv-tabs">
          {(["set", "vis", "emo", ...(visualEditable ? ["assets"] as Tab[] : []), "world"] as Tab[]).map((k) => (
            <div
              key={k}
              className={"cv-tab" + (tab === k ? " on" : "")}
              onClick={() => { switchTab(k); if (editable) liveSaveDebounced(); }}
            >
              {t(("cv-tab-" + k) as TKey)}
            </div>
          ))}
        </div>

        <div className="cv-scroll">
          {/* 设定 */}
          {tab === "set" && (
            <div className="cv-pane">
              {(editable || full.description) && (
                <CardBlock labelKey="cve-description" hub={hub} ctx={editorCtx} fieldRef={fDesc} fieldKey={editable ? "description" : undefined} badge={soulBadge}
                  field={<CardField ref={fDesc} editable={editable} initial={seed("description", full.description || "")} />} />
              )}
              {(editable || full.personality) && (
                <CardBlock labelKey="cve-personality" hub={hub} ctx={editorCtx} fieldRef={fPers} fieldKey={editable ? "personality" : undefined} badge={soulBadge}
                  field={<CardField ref={fPers} editable={editable} initial={seed("personality", full.personality || "")} />} />
              )}
              {(editable || full.scenario) && (
                <CardBlock labelKey="cve-scenario" hub={hub} ctx={editorCtx} fieldRef={fScen} fieldKey={editable ? "scenario" : undefined} badge={soulBadge}
                  field={<CardField ref={fScen} editable={editable} initial={seed("scenario", full.scenario || "")} />} />
              )}
              {(editable || full.first_mes) && (
                <CardBlock labelKey="cv-first" hub={hub} ctx={editorCtx} fieldRef={fFirst} fieldKey={editable ? "first_mes" : undefined} badge={soulBadge}
                  field={<CardField ref={fFirst} editable={editable} initial={seed("first_mes", full.first_mes || "")} />} />
              )}
              {(editable || goalsText) && (
                <CardBlock labelKey="cve-goals" hub={hub} ctx={editorCtx} fieldRef={fGoals} fieldKey={editable ? "goals" : undefined} badge={aspBadge}
                  field={<CardField ref={fGoals} editable={editable} initial={seed("goals", goalsText)} />} />
              )}
              {(editable || full.creator_notes) && (
                <CardBlock labelKey="cve-notes" hub={hub} ctx={editorCtx} fieldRef={fNotes} badge={soulBadge}
                  field={<CardField ref={fNotes} editable={editable} initial={seed("creator_notes", full.creator_notes || "")} />} />
              )}
              {full.raw && (
                <details className="cv-raw">
                  <summary>{t("cv-raw")}</summary>
                  <pre>{JSON.stringify(full.raw, null, 2)}</pre>
                </details>
              )}
            </div>
          )}

          {/* 视觉 */}
          {tab === "vis" && (
            <div className="cv-pane">
              {/* R9 visual-set editor: generate / upload / replace / delete per kind,
                  reference tray, one-click generate-all. Builtin/locked/PNG cards are
                  read-only (the editor disables its controls; fall back to read tiles). */}
              {isJson ? (
                <VisualEditor cardPath={card.path} card={cardForVisual} disabled={!visualEditable} onChanged={onChanged} />
              ) : hasAnyArt ? (
                <div className="cv-tiles">
                  <ArtTile labelKey="cv-art-sprite" url={cardForVisual.sprite_url} name={charName} />
                  <ArtTile labelKey="cv-art-keyvisual" url={cardForVisual.keyvisual_url} name={charName} />
                  <ArtTile labelKey="cv-art-bg" url={cardForVisual.bg_url} name={charName} sq />
                  <ArtTile labelKey="cv-art-avatar" url={avatarSrc(card)} name={charName} sq />
                </div>
              ) : (
                <div className="cv-empty">
                  <div className={"cv-empty-art " + paletteClass(charName)}>
                    <div className="cv-art-glyph">{glyphOf(charName)}</div>
                  </div>
                  <div className="cv-empty-note">{t("cv-no-art")}</div>
                </div>
              )}
              {(editable || th.primary || th.secondary) && (
                <div className="cv-themebar">
                  <b>{t("cv-theme-label")}</b>
                  {editable ? (
                    <>
                      {/* editable: native color pickers, committed on blur via card.patch */}
                      <label className="cv-swatch cv-swatch-edit">
                        <input type="color" defaultValue={th.primary || "#5b9fd4"}
                          onBlur={(e) => void patchExt({ theme: { primary: e.target.value } })} />
                        <span>{t("cv-theme-primary")}</span>
                      </label>
                      <label className="cv-swatch cv-swatch-edit">
                        <input type="color" defaultValue={th.secondary || "#888888"}
                          onBlur={(e) => void patchExt({ theme: { secondary: e.target.value } })} />
                        <span>{t("cv-theme-secondary")}</span>
                      </label>
                    </>
                  ) : (
                    <>
                      {th.primary && (
                        <div className="cv-swatch">
                          <i style={{ background: th.primary }} />
                          <span>{t("cv-theme-primary")} {th.primary}</span>
                        </div>
                      )}
                      {th.secondary && (
                        <div className="cv-swatch">
                          <i style={{ background: th.secondary }} />
                          <span>{t("cv-theme-secondary")} {th.secondary}</span>
                        </div>
                      )}
                    </>
                  )}
                  <span className="cv-theme-note">{t("cv-theme-note")}</span>
                </div>
              )}
            </div>
          )}

          {/* 表情 */}
          {tab === "emo" && (
            <div className="cv-pane">
              {Array.isArray(card.stickers_urls) && card.stickers_urls.filter(Boolean).length > 0 ? (
                <div className="cv-emos">
                  {card.stickers_urls.filter(Boolean).map((url, i) => (
                    <div className="cv-emo" key={i}>
                      <div className="cv-emo-pic" style={{ backgroundImage: `url("${assetUrl(String(url)).replace(/"/g, "%22")}")` }} />
                    </div>
                  ))}
                </div>
              ) : (
                <div className="cv-empty">
                  <div className={"cv-empty-art " + paletteClass(charName)}>
                    <div className="cv-art-glyph">{glyphOf(charName)}</div>
                  </div>
                  <div className="cv-empty-note">{t("cv-no-emo")}</div>
                </div>
              )}
            </div>
          )}

          {/* 素材 — extra images that travel with the card (refs/alternates), not the
              managed visual set. List/upload/delete, saved immediately. */}
          {tab === "assets" && visualEditable && (
            <div className="cv-pane">
              <AssetsPane cardPath={card.path} disabled={!visualEditable} />
            </div>
          )}

          {/* 世界 */}
          {tab === "world" && (
            <div className="cv-pane">
              {editable ? (
                <CardBlock labelKey="cve-world" hub={hub} ctx={editorCtx} fieldRef={fWorld} fieldKey="world_entries" badge={soulBadge}
                  field={<CardField ref={fWorld} editable initial={seed("world", worldText)} />} />
              ) : (
                <WorldReadOnly entries={book ? book.entries! : []} />
              )}
            </div>
          )}
        </div>

        <div className="cv-foot">
          {/* Everything auto-saves (card.patch on blur/tab-switch/close) — so there's no
              Save button and nothing "unsaved" to discard; the close is just 完成/关闭. */}
          <button className="btn text" onClick={closeEditor}>
            {editable ? t("cv-done") : t("cancel")}
          </button>
          <div className="grow" />
          {!card.builtin && !card.frozen && !liveCard && (
            <button className="btn soft" onClick={() => void doDelete()}>{t("menu-delete")}</button>
          )}
          {card.builtin && isJson && (
            <button className="btn soft" disabled={dupBusy} onClick={() => void doDuplicate()}>
              {dupBusy ? <span className="spin" /> : t("deck-copy")}
            </button>
          )}
          {!liveCard && (
            <button
              className="btn primary go"
              disabled={genRunning}
              title={genRunning ? t("wake-generating") : undefined}
              onClick={() => { void (editable ? liveSave() : Promise.resolve()).finally(() => { onClose(); onWake(card); }); }}
            >
              {genRunning ? t("wake-generating") : t("deck-wake")}
            </button>
          )}
        </div>
      </div>
    </DeckModal>
  );
}

interface CardAsset { rel: string; name: string; url: string | null; size: number; kind: string }

const KIND_GLYPH: Record<string, string> = {
  image: "🖼", audio: "🎵", video: "🎬", pdf: "📕", text: "📄", archive: "🗜", file: "📦",
};
function assetExt(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1).toUpperCase().slice(0, 4) : "";
}

/* 素材 manager — the card's extra files of ANY format (everything beside the card that
   isn't the managed visual set). Images thumbnail inline; other files show a glyph and
   download via card.asset_file_read. View / upload / delete, each saved immediately. */
function AssetsPane({ cardPath, disabled }: { cardPath: string; disabled: boolean }) {
  const t = useT();
  const { hub } = useHubApi();
  const [items, setItems] = useState<CardAsset[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const load = async () => {
    try {
      const r = await hub.call<{ assets?: CardAsset[] }>("card.assets_list", { path: cardPath }, 15000);
      setItems(r.assets || []);
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cardPath]);

  const onUpload = async (f: File) => {
    if (f.size > 32 * 1024 * 1024) { setErr(t("av-up-size")); return; }
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    setBusy(true); setErr("");
    try {
      const b64 = await fileToB64(f);
      await hub.call("card.asset_file_upload", { path: cardPath, name: f.name, data_b64: b64, ext }, 60000);
      await load();
      deckToast(t("saved"));
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
    } finally {
      setBusy(false);
    }
  };
  const onDelete = async (a: CardAsset) => {
    if (!confirm(t("vis-del-q"))) return;
    setBusy(true); setErr("");
    try {
      await hub.call("card.asset_file_delete", { path: cardPath, rel: a.rel }, 15000);
      await load();
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
    } finally {
      setBusy(false);
    }
  };
  const download = async (a: CardAsset) => {
    setErr("");
    try {
      let href = a.url ? assetUrl(a.url) : "";
      if (!href) {
        // the /asset route can't serve a non-image from a card/session dir → read it.
        const r = await hub.call<{ data_uri?: string; too_large?: boolean }>(
          "card.asset_file_read", { path: cardPath, rel: a.rel }, 60000);
        if (r.too_large) { setErr(t("cv-asset-toobig")); return; }
        href = r.data_uri || "";
      }
      if (!href) return;
      const el = document.createElement("a");
      el.href = href;
      el.download = a.name;
      document.body.appendChild(el);
      el.click();
      el.remove();
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
    }
  };

  return (
    <div className="cv-assets-mgr">
      <div className="av-note">{t("cv-assets-note")}</div>
      <div className="cv-assets-grid">
        {items.map((a) => (
          <div className="cv-asset" key={a.rel} title={`${a.name} · ${fmtSize(a.size)}`}>
            {a.kind === "image" && a.url ? (
              <img src={assetUrl(a.url)} alt="" onClick={() => void download(a)} />
            ) : (
              <button className="cv-asset-glyph" onClick={() => void download(a)} title={t("cv-asset-download")}>
                <span className="cv-asset-ic">{KIND_GLYPH[a.kind] || KIND_GLYPH.file}</span>
                <span className="cv-asset-ext">{assetExt(a.name)}</span>
              </button>
            )}
            {!disabled && (
              <button className="vis-cand-x" title={t("del-word")} disabled={busy} onClick={() => void onDelete(a)}>×</button>
            )}
            <span className="cv-asset-name">{a.name}</span>
          </div>
        ))}
        {!disabled && (
          <button className="cv-asset-add" disabled={busy} onClick={() => fileInput.current?.click()}>
            {busy ? <span className="spin" /> : "＋"}
          </button>
        )}
      </div>
      {items.length === 0 && disabled && <div className="cv-empty-note">{t("cv-assets-empty")}</div>}
      {err && <div className="av-note err">{err}</div>}
      <input
        ref={fileInput}
        type="file"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files && e.target.files[0];
          e.target.value = "";
          if (f) void onUpload(f);
        }}
      />
    </div>
  );
}

function ArtTile({
  labelKey,
  url,
  name,
  sq,
}: {
  labelKey: TKey;
  url?: string;
  name: string;
  sq?: boolean;
}) {
  const t = useT();
  return (
    <div className={"cv-tile" + (sq ? " sq" : "")}>
      <div
        className={"cv-art" + (url ? "" : " empty " + paletteClass(name))}
        style={url ? { backgroundImage: `url("${assetUrl(String(url)).replace(/"/g, "%22")}")` } : undefined}
      >
        {!url && <div className="cv-art-glyph">{glyphOf(name)}</div>}
      </div>
      <div className="cv-art-cap">
        <b>{t(labelKey)}</b>
        {!url && <span className="cv-art-none">{t("cv-art-none")}</span>}
      </div>
    </div>
  );
}

function WorldReadOnly({ entries }: { entries: WorldBookEntry[] }) {
  const t = useT();
  if (!entries.length) return <div className="cv-empty-note">{t("cv-world-empty")}</div>;
  return (
    <>
      {entries.map((e2, i) => {
        const keys = (e2.keys || []).slice(0, 5).join(" · ");
        return (
          <details className="cv-we" key={i} open={i === 0}>
            <summary>
              <span className={"cv-st " + (e2.constant ? "const" : "kw")}>
                {t(e2.constant ? "cv-world-const" : "cv-world-kw")}
              </span>
              {keys && <span className="cv-we-keys">{keys}</span>}
            </summary>
            <div className="cv-we-body">{String(e2.content || "")}</div>
          </details>
        );
      })}
    </>
  );
}
