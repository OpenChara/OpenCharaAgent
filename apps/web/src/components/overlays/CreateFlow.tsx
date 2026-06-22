/* CreateFlow — the tell→shape→land create-a-chara flow, a React port of app.js
 * openCreateFlow (2086) / renderTellStep (2391) / renderShapeStep (2454).
 *
 * Step 1 (tell): a free-text telling → cards.draft (the SYSTEM default model) →
 * normalizeDraft → step 2. Step 2 (shape): every section editable (with the same
 * inline ✦ AI rewrite as the card editor), the telling kept in a collapsible
 * panel, a shared avatar/theme editor, the embodiment stance — then either
 * card.from_draft as_draft (save-draft) or land the card + offer "wake now?".
 *
 * Binding UI rule: generation shows a ticking "思考 Ns" state and surfaces errors
 * with a retry; save buttons show working states. The telling never disappears. */

import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { useOverlay } from "../../state/overlay";
import { rpcErrText } from "../../lib/status";
import {
  normalizeDraft,
  sectionText,
  putSection,
  type NormalizedDraft,
} from "../../lib/cards";
import { CardField, CardBlock, cardCtxString, type FieldHandle } from "../deck/CardField";
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
  ["world_entries", "sec-world"],
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
  const defaults = (snapshot?.defaults as { model?: string }) || {};

  const [step, setStep] = useState<Step>("tell");
  const [origin, setOrigin] = useState("");
  const draftRef = useRef<NormalizedDraft | null>(null);

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
            defaultModel={String(defaults.model || "")}
            onClose={guardedClose}
            generate={async () => {
              const raw = await hub.call("cards.draft", { inspiration: origin.trim() }, 240000);
              draftRef.current = normalizeDraft(raw as Record<string, unknown>);
              setStep("shape");
            }}
            modelsList={() => hub.call<ModelInfo[]>("models.list", {}, 30000).catch(() => [] as ModelInfo[])}
          />
        ) : (
          <ShapeStep
            t={t}
            draft={draftRef.current!}
            origin={origin}
            onBack={() => setStep("tell")}
            saveDraft={async (data) => {
              draftRef.current = data;
              await hub.call("card.from_draft", { draft: data, origin, as_draft: true }, 30000);
              await injectExtras(hub, data, undefined);
            }}
            land={async (data) => {
              draftRef.current = data;
              const r = await hub.call<{ path: string }>("card.from_draft", { draft: data, origin }, 30000);
              await injectExtras(hub, data, r.path);
              await refresh();
              return r.path;
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

/* user_name / user_persona ride extensions.lunamoth (app.js injectUserFields).
   Best-effort: the card itself is already saved. Visuals (avatar/sprite/background)
   are no longer set here — they're done after the card lands, in the card editor's
   视觉 tab (the R9 VisualEditor). */
async function injectExtras(
  hub: ReturnType<typeof useHub>["hub"],
  draft: NormalizedDraft,
  path: string | undefined,
): Promise<void> {
  if (!path) return;
  if (!draft.user_name && !draft.user_persona) return;
  try {
    const full = await hub.call<{ raw?: { data?: Record<string, unknown> } }>("card.read", { path }, 20000);
    if (!full.raw || !full.raw.data) return;
    const ext = (full.raw.data.extensions = (full.raw.data.extensions as Record<string, unknown>) || {});
    const lm = ((ext as { lunamoth?: Record<string, unknown> }).lunamoth =
      ((ext as { lunamoth?: Record<string, unknown> }).lunamoth || {}) as Record<string, unknown>);
    lm.user_name = draft.user_name;
    lm.user_persona = draft.user_persona;
    await hub.call("card.save", { data: full.raw, path }, 20000);
  } catch {
    /* user fields are best-effort */
  }
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
  modelsList,
}: {
  t: ReturnType<typeof useT>;
  origin: string;
  setOrigin: (s: string) => void;
  hadDraft: boolean;
  defaultModel: string;
  onClose: () => void;
  generate: () => Promise<void>;
  modelsList: () => Promise<ModelInfo[]>;
}) {
  const [busy, setBusy] = useState(false);
  const [secs, setSecs] = useState(0);
  const [err, setErr] = useState("");
  const [noStar, setNoStar] = useState(false);
  const started = useRef(0);

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
      <div className="flow-inner">
        <div className="tell-guide">{t("tell-guide")}</div>
        <textarea
          className="tell-box"
          placeholder={t("tell-ph")}
          value={origin}
          autoFocus
          onChange={(e) => setOrigin(e.target.value)}
        />
        <div className="gen-model">{t("gen-with", { model: defaultModel || "—" })}</div>
        {noStar && <div className="cap-hint" style={{ marginTop: 10 }}>{t("tell-star-hint")}</div>}
        {busy && (
          <div className="transcribing">
            <i />
            <span className="think-elapsed">{t("thinking-n", { n: secs })}</span>
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
        <button className="btn primary big" disabled={busy} onClick={() => void run()}>
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
    data.force_roleplay = forceRoleplay;
    data.website = personalSite;
    return data;
  };

  const ctx = () => cardCtxString({ ...collect() });

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
          <div className="sec" key={key} data-sec={key}>
            <CardBlock
              labelKey={labelKey}
              hub={hub}
              ctx={ctx}
              fieldRef={secRefs.current[key]}
              fieldKey={key}
              field={<CardField ref={secRefs.current[key]} editable initial={sectionText(draft, key)} />}
            />
          </div>
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
