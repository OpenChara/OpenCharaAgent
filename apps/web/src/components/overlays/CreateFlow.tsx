/* CreateFlow — the tell→shape→land create-a-chara flow, a React port of app.js
 * openCreateFlow (2086) / renderTellStep (2391) / renderShapeStep (2454).
 *
 * Step 1 (tell): a free-text telling → cards.draft (the card-draft model — the
 * per-task `card_model`, else the main default — resolved server-side) →
 * normalizeDraft → step 2. Step 2 (shape): every section editable (with the same
 * inline ✦ AI rewrite as the card editor), the telling kept in a collapsible
 * panel, a shared avatar/theme editor, the embodiment stance — then either
 * card.from_draft as_draft (save-draft) or land the card + offer "wake now?".
 *
 * Binding UI rule: generation shows a ticking "思考 Ns" state and surfaces errors
 * with a retry; save buttons show working states. The telling never disappears. */

import { Fragment, useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { useOverlay } from "../../state/overlay";
import { rpcErrText } from "../../lib/status";
import {
  normalizeDraft,
  sectionText,
  putSection,
  toWorldEntries,
  looksLikeCardJson,
  type NormalizedDraft,
  type WorldEntryFull,
} from "../../lib/cards";
import { CardField, CardBlock, cardCtxString, type FieldHandle } from "../deck/CardField";
import { WorldBookEditor } from "../deck/WorldBookEditor";
import { fileToB64 } from "../../lib/file";
import { useDirtyGuard } from "../../hooks/useDirtyGuard";
import { DeckModal } from "../ui/DeckModal";
import { deckToast } from "../ui/deckToast";
import type { DeckCard, ModelInfo } from "../deck/types";
import type { TKey } from "../../i18n";

/** The card sections that carry the AI version-chain (app.js SECTION_DEFS). */
const SECTION_DEFS: ReadonlyArray<readonly [string, TKey]> = [
  ["description", "sec-description"],
  ["personality", "cve-personality"],
  ["scenario", "cve-scenario"],
  ["first_mes", "sec-first"],
  // world_entries is edited by the structured WorldBookEditor (not a text section).
  ["seed_goals", "sec-goals"],
  ["tagline", "sec-tagline"],
] as const;

type Step = "tell" | "shape";

function FlowSteps({ active }: { active: 0 | 1 }) {
  const t = useT();
  const names = [t("flow-tell"), t("flow-shape")];
  return (
    <div className="flow-steps">
      {names.map((n, i) => (
        <span key={i}>
          {i > 0 && <i />}
          {i < active ? <span className="done">✓ {n}</span> : i === active ? <b>{n}</b> : <span>{n}</span>}
        </span>
      ))}
    </div>
  );
}

export function CreateFlow({ onClose }: { onClose: () => void }) {
  const t = useT();
  const { hub, snapshot, refresh } = useHub();
  const nav = useNavigate();
  const overlay = useOverlay();
  const defaults = (snapshot?.defaults as { model?: string; card_model?: string }) || {};
  // cards.draft routes through the per-task card_model server-side (else the main
  // default), so the flow shows THAT model — not the main one — as "生成模型".
  const draftModel = String(defaults.card_model || defaults.model || "");

  const [step, setStep] = useState<Step>("tell");
  const [origin, setOrigin] = useState("");
  const draftRef = useRef<NormalizedDraft | null>(null);
  // The deck path this draft lives at, captured from the first save. Every
  // later from_draft (re-save OR land) reuses it so we OVERWRITE one file
  // instead of letting save_card auto-name a `-2` duplicate — that left a
  // stray un-woken draft beside the landed/woken card.
  const draftPathRef = useRef<string>("");
  // Save-draft and land are the same write (card.from_draft persists the whole
  // card, user_name/user_persona included); they differ only by the as_draft flag
  // and that land refreshes + returns. One helper keeps the path-reuse contract
  // (capture r.path back into the ref) in a single place so it can't drift.
  const fromDraft = async (data: NormalizedDraft, asDraft: boolean): Promise<string> => {
    draftRef.current = data;
    const r = await hub.call<{ path: string }>(
      "card.from_draft",
      { draft: data, origin, as_draft: asDraft || undefined, path: draftPathRef.current || undefined },
      30000,
    );
    draftPathRef.current = r.path;
    return r.path;
  };

  // Faithful import → land on the deck. Shared by the pasted-JSON banner and a dragged-in
  // file (.json card text, or a .png card whose embedded portrait also becomes the avatar).
  const importForeign = async (params: { text?: string; png_b64?: string }) => {
    const r = await hub.call<{ name?: string }>("cards.import_foreign", params, 30000);
    await refresh();
    onClose();
    deckToast(t("create-import-done", { name: r?.name || "" }));
    nav("#/deck");
  };
  const importFile = async (file: File) => {
    const lower = file.name.toLowerCase();
    if (lower.endsWith(".png") || file.type === "image/png") {
      const b64 = await fileToB64(file);
      if (!b64) throw new Error("could not read the file");
      await importForeign({ png_b64: b64 });
    } else {
      await importForeign({ text: await file.text() });
    }
  };

  // Dirty-guard (shared hook): typing the telling or any shape-step edit flags
  // dirty, so a stray Esc/backdrop/Cancel can't throw away a half-built character.
  const { guardedClose, dirtyProps } = useDirtyGuard(onClose);

  return (
    <DeckModal open variant="wide" onClose={guardedClose}>
      <div className="flow" {...dirtyProps}>
        {step === "tell" ? (
          <TellStep
            t={t}
            origin={origin}
            setOrigin={setOrigin}
            hadDraft={!!draftRef.current}
            defaultModel={draftModel}
            onClose={guardedClose}
            generate={async () => {
              const raw = await hub.call("cards.draft", { inspiration: origin.trim() }, 240000);
              draftRef.current = normalizeDraft(raw as Record<string, unknown>);
              setStep("shape");
            }}
            // Faithful import (no model call — persona/greeting/lorebook verbatim), then
            // land on the deck. Used by both the pasted-JSON banner and a dragged-in file.
            importJson={(text) => importForeign({ text })}
            importFile={importFile}
            modelsList={() => hub.call<{ models?: ModelInfo[] }>("models.list", {}, 30000).then((r) => r?.models ?? []).catch(() => [] as ModelInfo[])}
          />
        ) : (
          <ShapeStep
            t={t}
            draft={draftRef.current!}
            origin={origin}
            onBack={() => setStep("tell")}
            saveDraft={async (data) => {
              await fromDraft(data, true);
            }}
            land={async (data) => {
              const path = await fromDraft(data, false);
              await refresh();
              return path;
            }}
            afterLand={(path, name) => {
              onClose();
              const cards = (snapshot?.cards as DeckCard[] | undefined) || [];
              const card = cards.find((c) => c.path === path) || ({ path, name, lang: "zh" } as DeckCard);
              // "Wake now?" hand-off (app.js: openModal → openWakeSheet | navTo deck).
              overlay.open({ kind: "wake", card });
            }}
            toDeck={() => {
              onClose();
              nav("#/deck");
            }}
            refresh={refresh}
            hub={hub}
          />
        )}
      </div>
    </DeckModal>
  );
}

/* ------------------------------ TELL STEP ------------------------------ */
function TellStep({
  t,
  origin,
  setOrigin,
  hadDraft,
  defaultModel,
  onClose,
  generate,
  importJson,
  importFile,
  modelsList,
}: {
  t: ReturnType<typeof useT>;
  origin: string;
  setOrigin: (s: string) => void;
  hadDraft: boolean;
  defaultModel: string;
  onClose: () => void;
  generate: () => Promise<void>;
  importJson: (text: string) => Promise<void>;
  importFile: (file: File) => Promise<void>;
  modelsList: () => Promise<ModelInfo[]>;
}) {
  const [busy, setBusy] = useState(false);
  const [secs, setSecs] = useState(0);
  const [err, setErr] = useState("");
  const [noStar, setNoStar] = useState(false);
  const [importing, setImporting] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const started = useRef(0);
  const isCard = looksLikeCardJson(origin);

  // One import runner for both the pasted-JSON banner and a dropped file. A thrown import
  // leaves the flow open with the error (retry); a success unmounts us (lands on deck).
  const runImport = async (go: () => Promise<void>) => {
    setErr("");
    setImporting(true);
    try {
      await go();
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
      setImporting(false);
    }
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer?.files?.[0];
    if (file && !importing) void runImport(() => importFile(file));
  };

  // gentle writing-star hint when the default model lacks ★ (app.js renderTellStep)
  useEffect(() => {
    let alive = true;
    void modelsList().then((models) => {
      const m = models.find((x) => x.id === defaultModel);
      if (alive && m && !m.writing) setNoStar(true);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultModel]);

  const run = async () => {
    const text = origin.trim();
    if (!text) return;
    if (hadDraft && !confirm(t("draft-overwrite-q"))) return;
    setErr("");
    setBusy(true);
    setSecs(0);
    started.current = Date.now();
    const tick = setInterval(() => setSecs(Math.floor((Date.now() - started.current) / 1000)), 1000);
    try {
      await generate();
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
    } finally {
      clearInterval(tick);
      setBusy(false);
    }
  };

  return (
    <>
      <FlowSteps active={0} />
      <div
        className={"flow-inner" + (dragOver ? " drag-over" : "")}
        onDragOver={(e) => {
          if (e.dataTransfer?.types?.includes("Files")) {
            e.preventDefault();
            setDragOver(true);
          }
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
        }}
        onDrop={onDrop}
      >
        {dragOver && <div className="flow-drop-veil">{t("create-drop-hint")}</div>}
        <div className="tell-guide">{t("tell-guide")}</div>
        <textarea
          className="tell-box"
          placeholder={t("tell-ph")}
          value={origin}
          autoFocus
          onChange={(e) => setOrigin(e.target.value)}
        />
        {isCard && (
          <div className="card-detected">
            <span>{t("create-card-detected")}</span>
            <button className="btn primary" disabled={importing} onClick={() => void runImport(() => importJson(origin))}>
              {importing ? <span className="spin" /> : t("create-import-faithful")}
            </button>
          </div>
        )}
        <div className="gen-model">{t("gen-with", { model: defaultModel || "—" })}</div>
        <div className="drop-hint">{t("create-drop-hint")}</div>
        {noStar && <div className="cap-hint" style={{ marginTop: 10 }}>{t("tell-star-hint")}</div>}
        {busy && (
          <div className="transcribing">
            <i />
            <span className="think-elapsed">{t("thinking-n", { n: secs })}</span>
          </div>
        )}
        {importing && !isCard && (
          <div className="transcribing">
            <i />
            <span>{t("create-importing")}</span>
          </div>
        )}
        {err && (
          <div className="draft-error">
            <b>{err}</b>
            <button className="btn soft" onClick={() => void run()}>{t("retry")}</button>
          </div>
        )}
      </div>
      <div className="flow-bar">
        <button className="btn text" onClick={onClose}>{t("cancel")}</button>
        <div className="grow" />
        <button className={"btn big " + (isCard ? "soft" : "primary")} disabled={busy || importing} onClick={() => void run()}>
          {busy ? <span className="spin" /> : t("tell-go-edit")}
        </button>
      </div>
    </>
  );
}

/* ------------------------------ SHAPE STEP ------------------------------ */
function ShapeStep({
  t,
  draft,
  origin,
  onBack,
  saveDraft,
  land,
  afterLand,
  toDeck,
  refresh,
  hub,
}: {
  t: ReturnType<typeof useT>;
  draft: NormalizedDraft;
  origin: string;
  onBack: () => void;
  saveDraft: (data: NormalizedDraft) => Promise<void>;
  land: (data: NormalizedDraft) => Promise<string>;
  afterLand: (path: string, name: string) => void;
  toDeck: () => void;
  refresh: () => Promise<void>;
  hub: ReturnType<typeof useHub>["hub"];
}) {
  const [originOpen, setOriginOpen] = useState(false);
  const [forceRoleplay, setForceRoleplay] = useState(
    draft.force_roleplay === true || draft.embodiment === "actor",
  );
  // New charas default the website module ON (opt out, not in).
  const [personalSite, setPersonalSite] = useState(draft.website !== false);
  const [savingDraft, setSavingDraft] = useState(false);
  const [landing, setLanding] = useState(false);
  const [landed, setLanded] = useState<{ path: string; name: string } | null>(null);
  // World book: structured entries (seeded from the draft), edited by WorldBookEditor.
  const [worldEntries, setWorldEntries] = useState<WorldEntryFull[]>(() =>
    toWorldEntries(draft.world_entries),
  );
  const [worldGenBusy, setWorldGenBusy] = useState(false);

  // Field handles (uncontrolled, read on collect). name/user_name/user_persona are
  // plain; SECTION_DEFS get the AI rewrite.
  const fName = useRef<FieldHandle>(null);
  const fUserName = useRef<FieldHandle>(null);
  const fUserPersona = useRef<FieldHandle>(null);
  const secRefs = useRef<Record<string, React.RefObject<FieldHandle | null>>>(
    Object.fromEntries(SECTION_DEFS.map(([k]) => [k, { current: null } as React.RefObject<FieldHandle | null>])),
  );

  const collect = (): NormalizedDraft => {
    const data: NormalizedDraft = { ...draft };
    data.name = (fName.current?.value() ?? "").trim();
    data.user_name = (fUserName.current?.value() ?? "").trim();
    data.user_persona = (fUserPersona.current?.value() ?? "").trim();
    for (const [key] of SECTION_DEFS) {
      const text = secRefs.current[key].current?.value() ?? "";
      putSection(data, key, text);
    }
    // world_entries rides the structured editor state (not a text section).
    data.world_entries = worldEntries;
    data.force_roleplay = forceRoleplay;
    data.website = personalSite;
    return data;
  };

  const ctx = () => cardCtxString({ ...collect() });

  // ✦ AI world-book generation for the in-progress draft (uses the current shape).
  const genWorld = async (mode: "fresh" | "expand") => {
    setWorldGenBusy(true);
    try {
      const cur = collect();
      const r = await hub.call<{ entries?: WorldEntryFull[] }>(
        "card.generate_worldbook",
        {
          name: cur.name,
          description: String(cur.description || ""),
          personality: String(cur.personality || ""),
          scenario: String(cur.scenario || ""),
          first_mes: String(cur.first_mes || ""),
          existing: mode === "expand"
            ? worldEntries.map((e) => ({ keys: e.keys, content: e.content, constant: e.constant }))
            : [],
          mode,
        },
        240000,
      );
      const gen = toWorldEntries(r.entries || []);
      if (!gen.length) {
        deckToast(t("wb-gen-empty"), true);
        return;
      }
      setWorldEntries(mode === "expand" ? [...worldEntries, ...gen] : gen);
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setWorldGenBusy(false);
    }
  };

  const onSaveDraft = async () => {
    setSavingDraft(true);
    try {
      await saveDraft(collect());
      deckToast(t("saved"));
      await refresh();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setSavingDraft(false);
    }
  };

  const onLand = async () => {
    setLanding(true);
    try {
      const data = collect();
      const path = await land(data);
      setLanded({ path, name: data.name || "" });
    } catch (e) {
      setLanding(false);
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };

  // The "card made — wake now?" confirmation (app.js openModal after from_draft).
  if (landed) {
    return (
      <div className="flow-inner">
        <h2>{t("card-made")}</h2>
        <div className="sub">{t("wake-now-q")}</div>
        <div className="acts" style={{ marginTop: 14 }}>
          <button className="btn text" onClick={toDeck}>{t("later-deck")}</button>
          <div className="grow" />
          <button className="btn primary big" onClick={() => afterLand(landed.path, landed.name)}>
            {t("deck-wake")}
          </button>
        </div>
      </div>
    );
  }

  const plain = (ref: React.RefObject<FieldHandle | null>, labelKey: TKey, initial: string) => (
    <div className="sec">
      <h3>{t(labelKey)}</h3>
      <CardField ref={ref} editable initial={initial} />
    </div>
  );

  return (
    <>
      <FlowSteps active={1} />
      <div className="flow-inner">
        <div className={"origin-panel" + (originOpen ? " expanded" : "")} onClick={() => setOriginOpen((v) => !v)}>
          <div className="oh">
            {t("origin-title")}
            <span className="cue">{t("origin-cue")}</span>
          </div>
          <div className="ox">{origin}</div>
        </div>

        {Array.isArray(draft.notes) && (draft.notes as string[]).length > 0 && (
          <div className="draft-note">{(draft.notes as string[]).join(" · ")}</div>
        )}

        <div className="name-pair">
          {plain(fName, "sec-name", draft.name)}
          {plain(fUserName, "sec-user-name", draft.user_name)}
        </div>
        {plain(fUserPersona, "sec-user-persona", draft.user_persona)}

        {SECTION_DEFS.map(([key, labelKey]) => (
          <Fragment key={key}>
            <div className="sec" data-sec={key}>
              <CardBlock
                labelKey={labelKey}
                hub={hub}
                ctx={ctx}
                fieldRef={secRefs.current[key]}
                fieldKey={key}
                field={<CardField ref={secRefs.current[key]} editable initial={sectionText(draft, key)} />}
              />
            </div>
            {key === "first_mes" && (
              <div className="sec" data-sec="world_entries">
                <h3>{t("sec-world")}</h3>
                <WorldBookEditor
                  entries={worldEntries}
                  editable
                  onChange={setWorldEntries}
                  onGenerate={(mode) => void genWorld(mode)}
                  genBusy={worldGenBusy}
                />
              </div>
            )}
          </Fragment>
        ))}

        <div className="sec embodiment-sec">
          <h3>{t("sec-embodiment")}</h3>
          <div className="switch-row">
            <div className="tx">
              <b>{t("mod-roleplay")}</b>
              <small>{t("mod-roleplay-hint")}</small>
            </div>
            <button
              className={"switch" + (forceRoleplay ? " on" : "")}
              onClick={() => setForceRoleplay((v) => !v)}
            />
          </div>
          <div className="switch-row">
            <div className="tx">
              <b>{t("mod-website")}</b>
              <small>{t("mod-website-hint")}</small>
            </div>
            <button
              className={"switch" + (personalSite ? " on" : "")}
              onClick={() => setPersonalSite((v) => !v)}
            />
          </div>
        </div>
      </div>

      <div className="flow-bar">
        <button className="btn text" onClick={onBack}>{t("back")}</button>
        <div className="grow" />
        <button className="btn soft" disabled={savingDraft} onClick={() => void onSaveDraft()}>
          {savingDraft ? <span className="spin" /> : t("save-draft")}
        </button>
        <button className="btn primary" disabled={landing} onClick={() => void onLand()}>
          {landing ? <span className="spin" /> : t("next-card")}
        </button>
      </div>
    </>
  );
}
