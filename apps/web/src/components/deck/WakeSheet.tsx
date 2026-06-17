/* WakeSheet — the 2-step wake editor, a React port of app.js:1806 openWakeSheet.
 * Step 1 = content (the same editable fields as the card editor, each with its
 * inline ✦ AI rewrite); step 2 = settings (name / model + caps / toolpack /
 * embodiment / isolation / network). `session.wake` freezes the EDITED card as
 * the chara's own and routes to the chat.
 *
 * Binding UI rule: the wake button shows a working state and reverts on failure;
 * the model field live-renders capability badges + the no-tools warning. */

import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { rpcErrText } from "../../lib/status";
import { sectionText, serializeCardFields, type NormalizedDraft, type CardData } from "../../lib/cards";
import { CardField, CardBlock, cardCtxString, type FieldHandle } from "./CardField";
import { Caps } from "./Caps";
import { deckToast } from "../ui/deckToast";
import { DeckModal } from "../ui/DeckModal";
import type { DeckCard, FullCard, ModelInfo, ToolpackInfo, CardExtLunamoth } from "./types";

const ISO_OPTS: ReadonlyArray<readonly [string, string, string]> = [
  ["dir", "iso-dir", "iso-dir-d"],
  ["sandbox", "iso-sandbox", "iso-sandbox-d"],
  ["docker", "iso-docker", "iso-docker-d"],
] as const;

export function WakeSheet({ card, onClose }: { card: DeckCard; onClose: () => void }) {
  const t = useT();
  const { hub, snapshot, refresh } = useHub();
  const nav = useNavigate();
  const defaults = (snapshot?.defaults as { model?: string }) || {};

  const [loaded, setLoaded] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [packs, setPacks] = useState<ToolpackInfo[] | null>(null);
  const [cardPack, setCardPack] = useState("");

  // step-2 form state
  const [name, setName] = useState(card.name);
  const [model, setModel] = useState("");
  const [pack, setPack] = useState("sandbox");
  const [emb, setEmb] = useState<"literal" | "actor">(card.embodiment === "actor" ? "actor" : "literal");
  const [iso, setIso] = useState("sandbox");
  const [wantNet, setWantNet] = useState(true); // ON by default at wake (matches runtime default)
  const [waking, setWaking] = useState(false);

  // step-1 content fields (uncontrolled, read on wake)
  const rawRef = useRef<{ name?: string; data?: Record<string, unknown> }>({ data: {} });
  const charNameRef = useRef(card.name);
  const dirty = useRef(false);
  const fName = useRef<FieldHandle>(null);
  const fUserName = useRef<FieldHandle>(null);
  const fUserPersona = useRef<FieldHandle>(null);
  const fDesc = useRef<FieldHandle>(null);
  const fPers = useRef<FieldHandle>(null);
  const fScen = useRef<FieldHandle>(null);
  const fFirst = useRef<FieldHandle>(null);
  const fTagline = useRef<FieldHandle>(null);
  const fGoals = useRef<FieldHandle>(null);
  const fWorld = useRef<FieldHandle>(null);
  const fOnAttach = useRef<FieldHandle>(null);
  const fOnDetach = useRef<FieldHandle>(null);
  const [initial, setInitial] = useState<Record<string, string>>({});

  useEffect(() => {
    let alive = true;
    (async () => {
      const [ml, full, tp] = await Promise.all([
        hub.call<ModelInfo[]>("models.list", {}, 30000).catch(() => [] as ModelInfo[]),
        hub.call<FullCard>("card.read", { path: card.path }, 20000).catch(() => null),
        hub.call<ToolpackInfo[]>("toolpacks.list", {}, 15000).catch(() => null),
      ]);
      if (!alive) return;
      setModels(Array.isArray(ml) ? ml : []);
      setPacks(Array.isArray(tp) && tp.length ? tp : null);
      setModel(String(defaults.model || ""));
      const rawCard = (full && full.raw) || { name: card.name, data: {} };
      if (!rawCard.data) rawCard.data = {};
      rawRef.current = rawCard;
      const ext0 = ((rawCard.data?.extensions as { lunamoth?: CardExtLunamoth })?.lunamoth || {}) as CardExtLunamoth;
      const charName = (full && full.name) || card.name;
      charNameRef.current = charName;
      const pk = String(ext0.toolpack || "");
      setCardPack(pk);
      setPack(pk || "sandbox");
      const book =
        full && full.character_book && Array.isArray(full.character_book.entries)
          ? full.character_book
          : null;
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
      const wishesSrc = Array.isArray(ext0.wishes) ? ext0.wishes : Array.isArray(ext0.goals) ? ext0.goals : [];
      setInitial({
        name: charName,
        user_name: String(ext0.user_name || ""),
        user_persona: String(ext0.user_persona || ""),
        description: (full && full.description) || "",
        personality: (full && full.personality) || "",
        scenario: (full && full.scenario) || "",
        first_mes: (full && full.first_mes) || "",
        tagline: String(ext0.tagline || card.tagline || ""),
        goals: wishesSrc.map(String).join("\n"),
        world: worldText,
        on_attach: String(ext0.on_attach || ""),
        on_detach: String(ext0.on_detach || ""),
      });
      setLoaded(true);
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.path, defaults.model]);

  const wakeCtx = () =>
    cardCtxString({
      name: fName.current?.value(),
      description: fDesc.current?.value(),
      personality: fPers.current?.value(),
      scenario: fScen.current?.value(),
      tagline: fTagline.current?.value(),
    });

  function collectCardData(): { name?: string; data?: Record<string, unknown> } {
    const raw = rawRef.current;
    const data = (raw.data = raw.data || {});
    // The wake step-1 editor DOES render user_name/user_persona/toolpack, so it
    // passes them as strings. serializeCardFields is the ONE shared, tested
    // serializer (also used by the card editor) — no hand-rolled assembly here.
    serializeCardFields(
      data as CardData,
      {
        name: fName.current?.value() ?? "",
        description: fDesc.current?.value() ?? "",
        personality: fPers.current?.value() ?? "",
        scenario: fScen.current?.value() ?? "",
        first_mes: fFirst.current?.value() ?? "",
        user_name: fUserName.current?.value() ?? "",
        user_persona: fUserPersona.current?.value() ?? "",
        tagline: fTagline.current?.value() ?? "",
        on_attach: fOnAttach.current?.value() ?? "",
        on_detach: fOnDetach.current?.value() ?? "",
        goals: fGoals.current?.value() ?? "",
        world: fWorld.current?.value() ?? "",
        toolpack: pack,
      },
      charNameRef.current,
    );
    raw.name = data.name as string;
    return raw;
  }

  const doWake = async () => {
    setWaking(true);
    try {
      const entry = await hub.call<{ name: string }>(
        "session.wake",
        {
          card: card.path,
          name: name.trim(),
          isolation: iso,
          model: model.trim(),
          toolpack: pack.trim() || "sandbox",
          embodiment: emb,
          card_data: collectCardData(),
        },
        60000,
      );
      onClose();
      await refresh();
      nav(`#/chara/${encodeURIComponent(entry.name)}`);
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
      setWaking(false);
    }
  };

  const charName = charNameRef.current;
  const modelInfo = models.find((m) => m.id === model.trim());

  // Dirty-guard: edits bubble to onInput/onChange; never close mid-wake, and warn
  // before discarding unsaved edits on a stray Esc/backdrop/Cancel.
  const guardedClose = () => {
    if (waking) return;
    if (dirty.current && !confirm(t("discard-edits-q"))) return;
    onClose();
  };

  return (
    <DeckModal open variant="wide" onClose={guardedClose}>
      {!loaded ? (
        <div className="wake-loading">
          <span className="spin" /> {t("thinking-live")}
        </div>
      ) : (
        <div onInput={() => (dirty.current = true)} onChange={() => (dirty.current = true)}>
          <div className="wake-steps">
            <i className={step === 1 ? "on" : "done"} />
            <i className={step === 2 ? "on" : ""} />
          </div>
          {step === 1 ? (
            <>
              <h2>{t("wake-edit-title", { name: charName })}</h2>
              <div className="sub">{t("wake-edit-sub")}</div>
              <div className="wake-content">
                <CardBlock labelKey="sec-name" hub={hub} ctx={wakeCtx} fieldRef={fName}
                  field={<CardField ref={fName} editable initial={initial.name} className="cve-name" />} />
                <CardBlock labelKey="sec-user-name" hub={hub} ctx={wakeCtx} fieldRef={fUserName} fieldKey="user_name"
                  field={<CardField ref={fUserName} editable initial={initial.user_name} placeholder={t("sec-user-name")} />} />
                <CardBlock labelKey="sec-user-persona" hub={hub} ctx={wakeCtx} fieldRef={fUserPersona} fieldKey="user_persona"
                  field={<CardField ref={fUserPersona} editable initial={initial.user_persona} />} />
                <CardBlock labelKey="cve-description" hub={hub} ctx={wakeCtx} fieldRef={fDesc} fieldKey="description"
                  field={<CardField ref={fDesc} editable initial={initial.description} />} />
                <CardBlock labelKey="cve-personality" hub={hub} ctx={wakeCtx} fieldRef={fPers} fieldKey="personality"
                  field={<CardField ref={fPers} editable initial={initial.personality} />} />
                <CardBlock labelKey="cve-scenario" hub={hub} ctx={wakeCtx} fieldRef={fScen} fieldKey="scenario"
                  field={<CardField ref={fScen} editable initial={initial.scenario} />} />
                <CardBlock labelKey="cv-first" hub={hub} ctx={wakeCtx} fieldRef={fFirst} fieldKey="first_mes"
                  field={<CardField ref={fFirst} editable initial={initial.first_mes} />} />
                <CardBlock labelKey="sec-tagline" hub={hub} ctx={wakeCtx} fieldRef={fTagline} fieldKey="tagline"
                  field={<CardField ref={fTagline} editable initial={initial.tagline} placeholder={t("sec-tagline")} />} />
                <CardBlock labelKey="cve-goals" hub={hub} ctx={wakeCtx} fieldRef={fGoals} fieldKey="goals"
                  field={<CardField ref={fGoals} editable initial={initial.goals} />} />
                <CardBlock labelKey="cve-world" hub={hub} ctx={wakeCtx} fieldRef={fWorld} fieldKey="world_entries"
                  field={<CardField ref={fWorld} editable initial={initial.world} />} />
                <CardBlock labelKey="cve-on-attach" hub={hub} ctx={wakeCtx} fieldRef={fOnAttach} fieldKey="on_attach"
                  field={<CardField ref={fOnAttach} editable initial={initial.on_attach} placeholder={t("cve-presence-ph")} />} />
                <CardBlock labelKey="cve-on-detach" hub={hub} ctx={wakeCtx} fieldRef={fOnDetach} fieldKey="on_detach"
                  field={<CardField ref={fOnDetach} editable initial={initial.on_detach} placeholder={t("cve-presence-ph")} />} />
              </div>
              <div className="acts" style={{ marginTop: 18 }}>
                <button className="btn text" onClick={guardedClose}>{t("cancel")}</button>
                <div className="grow" />
                <button className="btn primary big" onClick={() => setStep(2)}>{t("wake-continue")}</button>
              </div>
            </>
          ) : (
            <>
              <h2>{t("wake-title", { name: charName })}</h2>
              <div className="sub">{t("wake-sub")}</div>
              <div className="wake-settings">
                <div className="field-row">
                  <label>{t("wake-name")}</label>
                  <div className="input-like">
                    <input value={name} onChange={(e) => setName(e.target.value)} />
                  </div>
                </div>
                <div className="field-row">
                  <label>{t("wake-model")}</label>
                  <div className="input-like">
                    <input list="model-list" value={model} onChange={(e) => setModel(e.target.value)} />
                  </div>
                  <datalist id="model-list">
                    {models.slice(0, 400).map((m) => (
                      <option key={m.id} value={m.id} />
                    ))}
                  </datalist>
                  <Caps caps={modelInfo ? { tools: modelInfo.tools, writing: modelInfo.writing, vision: modelInfo.vision } : null} style={{ margin: "6px 0 0" }} />
                  {modelInfo && modelInfo.tools === false && <div className="amber-note">{t("wake-no-tools")}</div>}
                </div>
                <div className="field-row">
                  <label>{t("wake-toolpack")}</label>
                  <div className="input-like">
                    <input value={pack} onChange={(e) => setPack(e.target.value)} list={packs ? undefined : "toolpack-list"} />
                    {cardPack && <span className="cue">{t("wake-toolpack-card", { name: cardPack })}</span>}
                  </div>
                  {packs ? (
                    <div className="pack-list">
                      {packs.map((p) => (
                        <div
                          key={p.name}
                          className={"pack-option" + (pack.trim() === p.name ? " on" : "")}
                          onClick={() => setPack(p.name)}
                        >
                          <div className="pack-head">
                            <b>{p.name}</b>
                            {p.description && <span>{p.description}</span>}
                          </div>
                          {Array.isArray(p.tools) && p.tools.length > 0 && (
                            <div className="tool-chips">
                              {p.tools.slice(0, 10).map((tn) => (
                                <span key={tn} className="chip">{tn}</span>
                              ))}
                              {p.tools.length > 10 && <span className="chip">+{p.tools.length - 10}</span>}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <datalist id="toolpack-list">
                      {[...new Set(["sandbox", cardPack].filter(Boolean))].map((v) => (
                        <option key={v} value={v} />
                      ))}
                    </datalist>
                  )}
                </div>
                <div className="field-row">
                  <label>{t("wake-emb")}</label>
                  <div className="embodiment-grid">
                    {(["literal", "actor"] as const).map((mode) => (
                      <div key={mode} className={"emb-option" + (emb === mode ? " on" : "")} onClick={() => setEmb(mode)}>
                        <b>{mode}</b>
                        <span>{t("emb-" + mode)}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="field-row">
                  <label>{t("wake-iso")}</label>
                  <div className="iso-seg">
                    {ISO_OPTS.map(([key, label, desc]) => (
                      <div key={key} className={key === iso ? "on" : ""} onClick={() => setIso(key)}>
                        <b>{t(label)}</b>
                        <span>{t(desc)}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="field-row">
                  <div className="switch-row" style={{ fontSize: "12.5px" }}>
                    <b style={{ fontWeight: 550 }}>{t("p-net")}</b>
                    <small>{t("p-net-sub")}</small>
                    <button className={"switch" + (wantNet ? " on" : "")} onClick={() => setWantNet((v) => !v)} />
                  </div>
                </div>
              </div>
              <div className="acts" style={{ marginTop: 18 }}>
                <button className="btn text" onClick={() => setStep(1)}>{t("wake-back")}</button>
                <div className="grow" />
                <button className="btn primary big" disabled={waking} onClick={() => void doWake()}>
                  {waking ? <span className="spin" /> : t("wake-go")}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </DeckModal>
  );
}
