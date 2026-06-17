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
import { useHubApi } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { glyphOf, paletteClass } from "../../lib/format";
import { sectionText, serializeCardFields, type NormalizedDraft, type CardData } from "../../lib/cards";
import { CardField, CardBlock, cardCtxString, type FieldHandle } from "./CardField";
import { useDirtyGuard } from "../../hooks/useDirtyGuard";
import { Avatar, avatarSrc, themeOf, themeStyle } from "./visual";
import { VisualEditor } from "./VisualEditor";
import { deckToast, deckToastAction } from "../ui/deckToast";
import { DeckModal } from "../ui/DeckModal";
import type { DeckCard, FullCard, CardExtLunamoth, WorldBookEntry } from "./types";

type Tab = "set" | "vis" | "emo" | "world";

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
  const { hub } = useHubApi();
  const [full, setFull] = useState<FullCard | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("set");
  const [saving, setSaving] = useState(false);
  const [dupBusy, setDupBusy] = useState(false);

  const fName = useRef<FieldHandle>(null);
  const fTagline = useRef<FieldHandle>(null);
  const fDesc = useRef<FieldHandle>(null);
  const fPers = useRef<FieldHandle>(null);
  const fScen = useRef<FieldHandle>(null);
  const fFirst = useRef<FieldHandle>(null);
  const fGoals = useRef<FieldHandle>(null);
  const fNotes = useRef<FieldHandle>(null);
  const fWorld = useRef<FieldHandle>(null);
  const fOnAttach = useRef<FieldHandle>(null);
  const fOnDetach = useRef<FieldHandle>(null);

  // Dirty-guard (shared hook) — declared with the other hooks, before any early
  // return. A stray Esc/backdrop/Cancel can't silently drop a long card edit; never
  // closes mid save/duplicate. A successful save/delete closes via the raw onClose.
  const { guardedClose, dirtyProps } = useDirtyGuard(onClose, () => saving || dupBusy);

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
  const editable = !card.builtin && !card.locked && isJson;
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
  const wishesSrc = Array.isArray(ext.wishes) ? ext.wishes : Array.isArray(ext.goals) ? ext.goals : [];
  const goalsText = wishesSrc.map(String).join("\n");

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

  const doSave = async () => {
    if (!full.raw) return;
    setSaving(true);
    try {
      const data = (full.raw.data = (full.raw.data as CardData) || {});
      // The card editor edits the soul + presence + visuals notes; it deliberately
      // does NOT render user_name/user_persona/toolpack, so they're left undefined
      // (preserve). serializeCardFields is the ONE tested serializer (shared with
      // the wake sheet) — no hand-rolled lunamoth/world-book assembly here.
      serializeCardFields(
        data,
        {
          name: fName.current?.value() ?? "",
          description: fDesc.current?.value() ?? "",
          personality: fPers.current?.value() ?? "",
          scenario: fScen.current?.value() ?? "",
          first_mes: fFirst.current?.value() ?? "",
          creator_notes: fNotes.current?.value() ?? "",
          tagline: fTagline.current?.value() ?? "",
          on_attach: fOnAttach.current?.value() ?? "",
          on_detach: fOnDetach.current?.value() ?? "",
          goals: fGoals.current?.value() ?? "",
          world: fWorld.current?.value() ?? "",
        },
        full.name || "",
      );
      full.raw.name = data.name as string;
      await hub.call("card.save", { data: full.raw, path: card.path }, 20000);
      deckToast(t("saved"));
      onClose();
      onChanged();
    } catch (e) {
      setSaving(false);
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
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
  const goVisual = () => setTab("vis");

  return (
    <DeckModal open variant="cardview" onClose={guardedClose} style={themeStyle(card)}>
      <div className="cardview" {...dirtyProps}>
        {note && (
          <div className="cv-note cv-note-top">
            {note === "av-frozen-note" ? t(note, { names: (card.used_by || []).join("、") }) : t(note)}
          </div>
        )}
        <div className="cv-header">
          <Avatar name={charName} card={card} cls="avatar-s" onClick={goVisual} title={t("cv-tab-vis")} />
          <div className="cv-id">
            <CardField ref={fName} editable={editable} initial={full.name || ""} className="cve-name" />
            {(editable || taglineValue) && (
              <CardField ref={fTagline} editable={editable} initial={taglineValue} placeholder={t("sec-tagline")} className="tagline" />
            )}
            <div className="cv-badges">
              <span className="chip">{full.language || card.lang}</span>
              {card.builtin && <span className="chip">{t("deck-builtin")}</span>}
              {ext.embodiment && <span className="chip">{ext.embodiment}</span>}
              {ext.toolpack && <span className="chip">⚒ {ext.toolpack}</span>}
              {card.frozen && <span className="chip">{t("card-frozen-by", { names: (card.used_by || []).join("、") })}</span>}
            </div>
          </div>
        </div>

        <div className="cv-tabs">
          {(["set", "vis", "emo", "world"] as Tab[]).map((k) => (
            <div key={k} className={"cv-tab" + (tab === k ? " on" : "")} onClick={() => setTab(k)}>
              {t(("cv-tab-" + k) as TKey)}
            </div>
          ))}
        </div>

        <div className="cv-scroll">
          {/* 设定 */}
          {tab === "set" && (
            <div className="cv-pane">
              {(editable || full.description) && (
                <CardBlock labelKey="cve-description" hub={hub} ctx={editorCtx} fieldRef={fDesc} fieldKey={editable ? "description" : undefined}
                  field={<CardField ref={fDesc} editable={editable} initial={full.description || ""} />} />
              )}
              {(editable || full.personality) && (
                <CardBlock labelKey="cve-personality" hub={hub} ctx={editorCtx} fieldRef={fPers} fieldKey={editable ? "personality" : undefined}
                  field={<CardField ref={fPers} editable={editable} initial={full.personality || ""} />} />
              )}
              {(editable || full.scenario) && (
                <CardBlock labelKey="cve-scenario" hub={hub} ctx={editorCtx} fieldRef={fScen} fieldKey={editable ? "scenario" : undefined}
                  field={<CardField ref={fScen} editable={editable} initial={full.scenario || ""} />} />
              )}
              {(editable || full.first_mes) && (
                <CardBlock labelKey="cv-first" hub={hub} ctx={editorCtx} fieldRef={fFirst} fieldKey={editable ? "first_mes" : undefined}
                  field={<CardField ref={fFirst} editable={editable} initial={full.first_mes || ""} />} />
              )}
              {(editable || goalsText) && (
                <CardBlock labelKey="cve-goals" hub={hub} ctx={editorCtx} fieldRef={fGoals} fieldKey={editable ? "goals" : undefined}
                  field={<CardField ref={fGoals} editable={editable} initial={goalsText} />} />
              )}
              {(editable || full.creator_notes) && (
                <CardBlock labelKey="cve-notes" hub={hub} ctx={editorCtx} fieldRef={fNotes}
                  field={<CardField ref={fNotes} editable={editable} initial={full.creator_notes || ""} />} />
              )}
              {(editable || ext.on_attach || ext.on_detach) && (
                <details className="cv-raw">
                  <summary>{t("cve-advanced")}</summary>
                  <div className="cv-note">{t("cve-presence-help")}</div>
                  <CardBlock labelKey="cve-on-attach" hub={hub} ctx={editorCtx} fieldRef={fOnAttach} fieldKey={editable ? "on_attach" : undefined}
                    field={<CardField ref={fOnAttach} editable={editable} initial={String(ext.on_attach || "")} placeholder={t("cve-presence-ph")} />} />
                  <CardBlock labelKey="cve-on-detach" hub={hub} ctx={editorCtx} fieldRef={fOnDetach} fieldKey={editable ? "on_detach" : undefined}
                    field={<CardField ref={fOnDetach} editable={editable} initial={String(ext.on_detach || "")} placeholder={t("cve-presence-ph")} />} />
                </details>
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
                <VisualEditor cardPath={card.path} card={cardForVisual} disabled={!editable} onChanged={onChanged} />
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
              {(th.primary || th.secondary) && (
                <div className="cv-themebar">
                  <b>{t("cv-theme-label")}</b>
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

          {/* 世界 */}
          {tab === "world" && (
            <div className="cv-pane">
              {editable ? (
                <CardBlock labelKey="cve-world" hub={hub} ctx={editorCtx} fieldRef={fWorld} fieldKey="world_entries"
                  field={<CardField ref={fWorld} editable initial={worldText} />} />
              ) : (
                <WorldReadOnly entries={book ? book.entries! : []} />
              )}
            </div>
          )}
        </div>

        <div className="cv-foot">
          <button className="btn text" onClick={guardedClose}>{t("cancel")}</button>
          <div className="grow" />
          {!card.builtin && !card.frozen && (
            <button className="btn soft" onClick={() => void doDelete()}>{t("menu-delete")}</button>
          )}
          {card.builtin && isJson && (
            <button className="btn soft" disabled={dupBusy} onClick={() => void doDuplicate()}>
              {dupBusy ? <span className="spin" /> : t("deck-copy")}
            </button>
          )}
          {editable && (
            <button className="btn primary" disabled={saving} onClick={() => void doSave()}>
              {saving ? <span className="spin" /> : t("save")}
            </button>
          )}
          <button className="btn primary go" onClick={() => { onClose(); onWake(card); }}>{t("deck-wake")}</button>
        </div>
      </div>
    </DeckModal>
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
