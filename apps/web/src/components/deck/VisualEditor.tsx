/* VisualEditor — the R9 visual-set editor.
 *
 * Pipeline per card: ONE shared "image brief" (card.visual_brief) → per-kind
 * generation (card.visual_generate, now ASYNC: it returns a job_id and we poll
 * card.visual_job until ready). Generation is slow (30–240s), so every generate
 * shows a ticking progress state and never freezes the UI.
 *
 * Identity lock: the keyvisual is the ANCHOR — generate + confirm it first, and its
 * bytes are then fed as a reference into avatar / sprite / stickers / background so
 * the whole set looks like the same character. Skipping it is allowed, but a hint
 * recommends generating it first. A user reference tray (≤3) also guides generation.
 *
 * stickers (表情包) is a SET: one 3×3 sheet is generated, sliced into 9 cells server
 * side, and saved as a list via card.stickers_save. Other kinds are single images
 * (avatar → avatar_upload; sprite/background/keyvisual → asset_save).
 *
 * Each save writes to the card immediately, then refreshes the hub snapshot so the
 * new art shows. Builtin / locked / non-JSON cards are read-only (disabled). */

import { useEffect, useRef, useState } from "react";
import { assetUrl } from "../../rpc";
import { useT, type TFn, type TKey } from "../../i18n";
import { useHubApi, useHubState } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";
import { AVATAR_EXTS, AVATAR_UPLOAD_MAX } from "../overlays/avatar";
import { avatarSrc } from "./visual";
import type { DeckCard } from "./types";

const ART_EXTS = ["png", "jpg", "jpeg", "webp"];
const ART_UPLOAD_MAX = 16 * 1024 * 1024;
// The set shown + order. keyvisual is FIRST so it reads as the anchor to generate
// before the rest; stickers is the 9-expression set.
const DEFAULT_KINDS = ["keyvisual", "avatar", "sprite", "stickers", "background"] as const;
// Every kind can now be AI-generated (pipeline.KINDS covers all five).
const GENERATABLE: Record<string, boolean> = {
  keyvisual: true, avatar: true, sprite: true, stickers: true, background: true,
};
const REF_MAX = 3; // user reference images per generation
const REFS_CAP = 4; // anchor + user refs sent to the image model

type VisKind = "avatar" | "sprite" | "background" | "keyvisual" | "stickers";

interface Ref {
  data_b64: string;
  mime: string;
}

interface Brief {
  appearance?: string;
  style?: string;
  palette?: string;
  world?: string;
  theme?: string;
  [k: string]: unknown;
}

// The editable fields shown in the "image brief" panel (long → textarea). `style`
// is the LM-chosen rendering style — editing it is how a user overrides the look.
const BRIEF_FIELDS: { k: string; labelKey: TKey; long?: boolean }[] = [
  { k: "appearance", labelKey: "vis-brief-appearance", long: true },
  { k: "style", labelKey: "vis-brief-style", long: true },
  { k: "palette", labelKey: "vis-brief-palette" },
  { k: "world", labelKey: "vis-brief-world", long: true },
  { k: "theme", labelKey: "vis-brief-theme" },
];

interface GenResult {
  status?: string;
  data_b64?: string;
  stickers?: string[]; // present for kind=stickers (base64 PNG cells)
  mime?: string;
  ext?: string;
  kind?: string;
  matted?: boolean;
  note?: string;
  brief?: Brief;
}

type HubCall = <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/* Kick card.visual_generate (returns a job_id) then poll card.visual_job until the
   job is ready. A real failure surfaces as a rejected hubCall (structured error);
   "unknown" means the job expired. onTick reports elapsed seconds for the UI. */
async function runVisualJob(
  hubCall: HubCall,
  params: Record<string, unknown>,
  onTick?: (sec: number) => void,
): Promise<GenResult> {
  const sub = await hubCall<{ job_id?: string; status?: string }>("card.visual_generate", params, 30000);
  const jobId = sub?.job_id;
  if (!jobId) throw new Error("visual_generate did not return a job id");
  const t0 = Date.now();
  for (let i = 0; i < 400; i++) {
    await sleep(1500);
    onTick?.(Math.round((Date.now() - t0) / 1000));
    const st = await hubCall<GenResult & { status?: string }>("card.visual_job", { job_id: jobId }, 30000);
    if (st.status === "ready") return st;
    if (st.status === "unknown") throw new Error("the generation job expired — please try again");
    // "running" → keep polling
  }
  throw new Error("generation timed out");
}

/* Load an <img> from a src (data-URI), resolving once decoded. */
function loadImg(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const i = new Image();
    i.onload = () => res(i);
    i.onerror = () => rej(new Error("image load failed"));
    i.src = src;
  });
}

/* Downscale a (possibly large) image to fit the avatar's tiny budget, as PNG
   base64 — the generated avatar is ~1920² but the avatar sidecar is inlined into
   every hub.state, so it must stay small. */
async function downscalePngB64(dataUrl: string, maxDim: number): Promise<string> {
  const img = await loadImg(dataUrl);
  const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  if (ctx) ctx.drawImage(img, 0, 0, w, h);
  return c.toDataURL("image/png").split(",")[1] || "";
}

function fileToB64(f: File): Promise<string> {
  return new Promise((res, rej) => {
    const reader = new FileReader();
    reader.onload = () => res(String(reader.result || "").split(",")[1] || "");
    reader.onerror = () => rej(new Error("read failed"));
    reader.readAsDataURL(f);
  });
}

/* Fetch a same-origin asset URL and return it as a data-URI (so a previously-saved
   keyvisual can be reused as an identity reference — the image model needs the bytes
   inline, it can't reach a localhost /asset URL). */
async function urlToDataUri(url: string): Promise<string> {
  const resp = await fetch(url);
  const blob = await resp.blob();
  return await new Promise<string>((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result || ""));
    fr.onerror = () => rej(new Error("read failed"));
    fr.readAsDataURL(blob);
  });
}

function initUrlFor(kind: VisKind, card: DeckCard): string {
  if (kind === "avatar") return avatarSrc(card);
  if (kind === "sprite") return card.sprite_url ? assetUrl(String(card.sprite_url)) : "";
  if (kind === "keyvisual") return card.keyvisual_url ? assetUrl(String(card.keyvisual_url)) : "";
  return card.bg_url ? assetUrl(String(card.bg_url)) : "";
}

export function VisualEditor({
  cardPath,
  card,
  disabled,
  onChanged,
  kinds = DEFAULT_KINDS,
}: {
  cardPath: string;
  card: DeckCard;
  disabled: boolean;
  onChanged: () => void;
  /** which visual slots to show + order. Default = keyvisual/avatar/sprite/stickers/background. */
  kinds?: readonly VisKind[];
}) {
  const t = useT();
  const { hub, refresh } = useHubApi();
  const { snapshot } = useHubState();
  const nav = useNavigate();
  const hasImageKey = !!(snapshot?.defaults as { has_image_key?: boolean } | undefined)?.has_image_key;
  const [refs, setRefs] = useState<Ref[]>([]);
  const cachedBrief = useRef<Brief | null>(null);
  const refInput = useRef<HTMLInputElement>(null);
  const [briefOpen, setBriefOpen] = useState(false);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [briefBusy, setBriefBusy] = useState(false);
  const [briefErr, setBriefErr] = useState("");

  // Identity-lock anchor: the saved keyvisual's bytes (data-URI), reused as a
  // reference for the other kinds. A ref so generate-all reads it synchronously
  // right after the keyvisual slot saves; mirrored to state for the hint.
  const anchorRef = useRef<string | null>(null);
  const [anchorData, setAnchorData] = useState<string | null>(null);
  const setAnchor = (dataUri: string | null) => {
    anchorRef.current = dataUri;
    setAnchorData(dataUri);
  };

  const refData = () => refs.map((r) => `data:${r.mime};base64,${r.data_b64}`);
  // Per-kind references: keyvisual uses only user refs; every other kind gets the
  // keyvisual anchor prepended (when present) so the set stays one character.
  const refsFor = (kind: VisKind): string[] => {
    const user = refData();
    if (kind === "keyvisual") return user.slice(0, REFS_CAP);
    const a = anchorRef.current;
    return (a ? [a, ...user] : user).slice(0, REFS_CAP);
  };

  // Preload an existing keyvisual as the anchor (so a returning card keeps identity
  // lock without regenerating it). Best-effort; a failure just means no anchor.
  useEffect(() => {
    if (!kinds.includes("keyvisual") || anchorRef.current) return;
    const url = card.keyvisual_url ? assetUrl(String(card.keyvisual_url)) : "";
    if (!url) return;
    let live = true;
    void urlToDataUri(url)
      .then((d) => { if (live && d) setAnchor(d); })
      .catch(() => {});
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.keyvisual_url]);

  const loadBrief = async (force: boolean): Promise<Brief> => {
    if (cachedBrief.current && !force) {
      setBrief(cachedBrief.current);
      return cachedBrief.current;
    }
    setBriefBusy(true);
    setBriefErr("");
    try {
      const r = await hub.call<{ brief?: Brief }>("card.visual_brief", { path: cardPath }, 180000);
      const b = (r && r.brief) || {};
      cachedBrief.current = b;
      setBrief(b);
      return b;
    } catch (e) {
      setBriefErr(rpcErrText(t, e as { message?: string }));
      throw e;
    } finally {
      setBriefBusy(false);
    }
  };
  const getBrief = (): Promise<Brief> => loadBrief(false);
  const toggleBrief = () => {
    setBriefOpen((v) => !v);
    if (!cachedBrief.current && !briefBusy) void loadBrief(false).catch(() => {});
  };
  const editBrief = (k: string, v: string) => {
    const next: Brief = { ...(cachedBrief.current || {}), [k]: v };
    cachedBrief.current = next;
    setBrief(next);
  };

  // Background-removal readiness — only relevant when a sprite or stickers (kinds
  // that want a transparent cut) is in this set. Non-blocking nudge.
  const wantsCut = kinds.includes("sprite") || kinds.includes("stickers");
  const [matteReady, setMatteReady] = useState<boolean | null>(null);
  useEffect(() => {
    if (!wantsCut || !hasImageKey) return;
    let live = true;
    void hub
      .call<{ deps?: boolean; models?: { installed?: boolean }[] }>("matte.status", {}, 15000)
      .then((s) => { if (live) setMatteReady(!!(s.deps && (s.models || []).some((m) => m.installed))); })
      .catch(() => { if (live) setMatteReady(null); });
    return () => { live = false; };
  }, [wantsCut, hasImageKey, hub]);

  const addRef = async (f: File) => {
    if (refs.length >= REF_MAX) return;
    try {
      const b64 = await fileToB64(f);
      setRefs((cur) => (cur.length >= REF_MAX ? cur : [...cur, { data_b64: b64, mime: f.type || "image/png" }]));
    } catch {
      deckToast(t("av-up-read"), true);
    }
  };
  const removeRef = (i: number) => setRefs((cur) => cur.filter((_, j) => j !== i));

  // Slot refs so "generate all" can drive each slot's generate-and-save.
  const slotApi = useRef<Partial<Record<VisKind, (() => Promise<void>) | null>>>({});
  const [genAllBusy, setGenAllBusy] = useState(false);

  const genKinds = kinds.filter((k) => GENERATABLE[k]);
  // Generate the anchor (keyvisual) FIRST so the rest reference it.
  const orderedGenKinds = [
    ...genKinds.filter((k) => k === "keyvisual"),
    ...genKinds.filter((k) => k !== "keyvisual"),
  ];
  const showAnchorHint = hasImageKey && !disabled && kinds.includes("keyvisual") && !anchorData;

  const generateAll = async () => {
    if (!confirm(t("vis-gen-all-confirm", { n: orderedGenKinds.length }))) return;
    setGenAllBusy(true);
    try {
      for (const k of orderedGenKinds) {
        const fn = slotApi.current[k];
        if (fn) {
          try {
            await fn();
          } catch {
            /* the slot surfaces its own error */
          }
        }
      }
      deckToast(t("saved"));
      await refresh();
      onChanged();
    } finally {
      setGenAllBusy(false);
    }
  };

  return (
    <div className="vis-editor">
      {!hasImageKey && !disabled && (
        <div className="vis-invite">
          <div className="vis-invite-text">{t("vis-need-key")}</div>
          <button className="btn soft sm" onClick={() => nav("#/settings")}>
            {t("vis-need-key-cta")}
          </button>
        </div>
      )}

      {/* Identity-lock nudge: recommend generating the anchor (keyvisual) first. */}
      {showAnchorHint && (
        <div className="vis-invite">
          <div className="vis-invite-text">{t("vis-anchor-hint")}</div>
        </div>
      )}

      {/* Non-blocking matte nudge: a sprite/stickers cutout wants the model. */}
      {wantsCut && hasImageKey && matteReady === false && !disabled && (
        <div className="vis-invite">
          <div className="vis-invite-text">{t("vis-matte-hint")}</div>
          <button className="btn soft sm" onClick={() => nav("#/settings")}>
            {t("vis-matte-cta")}
          </button>
        </div>
      )}

      {/* The cached "image brief" — the shared visual description (incl. the chosen
          art style) reused across the whole set. Confirm/edit it before generating. */}
      {hasImageKey && (
        <div className="vis-brief-sec">
          <div className="vis-brief-head">
            <h4>{t("vis-brief-title")}</h4>
            <button className="btn text sm" disabled={disabled} onClick={toggleBrief}>
              {briefOpen ? t("vis-brief-hide") : t("vis-brief-edit")}
            </button>
          </div>
          <div className="av-note">{t("vis-brief-sub")}</div>
          {briefOpen && (
            <div className="vis-brief-body">
              {briefBusy && !brief ? (
                <div className="av-note thinking">{t("vis-brief-loading")}</div>
              ) : (
                <>
                  {BRIEF_FIELDS.map(({ k, labelKey, long }) => (
                    <label className="vis-brief-field" key={k}>
                      <span>{t(labelKey)}</span>
                      {long ? (
                        <textarea
                          value={String((brief?.[k] as string) || "")}
                          rows={k === "appearance" ? 4 : 2}
                          disabled={disabled || briefBusy}
                          onChange={(e) => editBrief(k, e.target.value)}
                        />
                      ) : (
                        <input
                          value={String((brief?.[k] as string) || "")}
                          disabled={disabled || briefBusy}
                          onChange={(e) => editBrief(k, e.target.value)}
                        />
                      )}
                    </label>
                  ))}
                  <div className="vis-brief-acts">
                    <button
                      className="btn soft sm"
                      disabled={disabled || briefBusy}
                      onClick={() => void loadBrief(true).catch(() => {})}
                    >
                      {briefBusy ? <span className="spin" /> : t("vis-brief-rebuild")}
                    </button>
                  </div>
                  {briefErr && <div className="av-note err">{briefErr}</div>}
                </>
              )}
            </div>
          )}
        </div>
      )}

      <div className="vis-ref-sec">
        <h4>{t("vis-ref-title")}</h4>
        <div className="av-note">{t("vis-ref-sub")}</div>
        <div className="vis-ref-tray">
          {refs.map((r, i) => (
            <div className="vis-ref" key={i}>
              <img src={`data:${r.mime};base64,${r.data_b64}`} alt="" />
              <button className="vis-ref-x" title={t("del-word")} onClick={() => removeRef(i)}>
                ×
              </button>
            </div>
          ))}
          {refs.length < REF_MAX && (
            <button className="vis-ref-add" title={t("vis-ref-add")} disabled={disabled} onClick={() => refInput.current?.click()}>
              +
            </button>
          )}
        </div>
        <input
          ref={refInput}
          type="file"
          accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files && e.target.files[0];
            e.target.value = "";
            if (f) void addRef(f);
          }}
        />
      </div>

      <div className="vis-slots">
        {kinds.map((kind) => (
          <VisualSlot
            key={kind}
            kind={kind}
            cardPath={cardPath}
            initUrl={initUrlFor(kind, card)}
            initSet={kind === "stickers" ? (card.stickers_urls || []).map((u) => assetUrl(String(u))) : []}
            disabled={disabled}
            canGenerate={hasImageKey && !!GENERATABLE[kind]}
            getBrief={getBrief}
            getRefs={() => refsFor(kind)}
            hubCall={hub.call.bind(hub)}
            refreshHub={refresh}
            onChanged={onChanged}
            onAnchor={setAnchor}
            t={t}
            registerGenerate={(fn) => {
              slotApi.current[kind] = fn;
            }}
          />
        ))}
      </div>

      {orderedGenKinds.length > 0 && (
        <div className="vis-all">
          <button className="btn primary" disabled={disabled || genAllBusy || !hasImageKey} onClick={() => void generateAll()}>
            {genAllBusy ? <span className="spin" /> : t("vis-gen-all", { n: orderedGenKinds.length })}
          </button>
          <div className="av-note" style={{ marginTop: 6 }}>
            {t("vis-gen-all-cost", { n: orderedGenKinds.length })}
          </div>
        </div>
      )}
    </div>
  );
}

function VisualSlot({
  kind,
  cardPath,
  initUrl,
  initSet,
  disabled,
  canGenerate,
  getBrief,
  getRefs,
  hubCall,
  refreshHub,
  onChanged,
  onAnchor,
  t,
  registerGenerate,
}: {
  kind: VisKind;
  cardPath: string;
  initUrl: string;
  initSet: string[];
  disabled: boolean;
  canGenerate: boolean;
  getBrief: () => Promise<Brief>;
  getRefs: () => string[];
  hubCall: HubCall;
  refreshHub: () => Promise<void>;
  onChanged: () => void;
  onAnchor: (dataUri: string | null) => void;
  t: TFn;
  registerGenerate: (fn: () => Promise<void>) => void;
}) {
  const isSet = kind === "stickers";
  const [curSrc, setCurSrc] = useState(initUrl);
  const [curSet, setCurSet] = useState<string[]>(initSet);
  // A generated-but-unsaved candidate staged for 保存 / 重新生成 / 取消.
  const [staged, setStaged] = useState<{ data_b64: string; mime: string; ext: string; note?: string } | null>(null);
  const [stagedSet, setStagedSet] = useState<string[] | null>(null);
  const [stagedNote, setStagedNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState("");
  const [errText, setErrText] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const previewSrc = staged ? `data:${staged.mime};base64,${staged.data_b64}` : curSrc;
  const previewSet = stagedSet ?? curSet;

  const setWorking = (msg: string) => {
    setBusy(true);
    setBusyMsg(msg);
    setErrText("");
  };
  const setIdle = () => {
    setBusy(false);
    setBusyMsg("");
  };
  const fail = (e: unknown) => {
    setBusy(false);
    setBusyMsg("");
    setErrText(rpcErrText(t, e as { message?: string }));
  };
  const tick = (sec: number) => setBusyMsg(t("vis-gen-progress", { n: sec }));

  // Save single-image bytes: avatar → downscaled PNG via avatar_upload; others →
  // asset_save in the true format. keyvisual additionally becomes the identity anchor.
  const saveBytes = async (data_b64: string, mime: string, ext: string) => {
    if (kind === "avatar") {
      const small = await downscalePngB64(`data:${mime || "image/png"};base64,${data_b64}`, 512);
      const r = await hubCall<{ data_uri?: string }>("card.avatar_upload", { path: cardPath, data_b64: small, ext: "png" }, 30000);
      if (r.data_uri) setCurSrc(r.data_uri);
    } else {
      const r = await hubCall<{ url?: string }>("card.asset_save", { path: cardPath, kind, data_b64, ext: ext || "png" }, 30000);
      if (r.url) setCurSrc(assetUrl(String(r.url)));
    }
    if (kind === "keyvisual") onAnchor(`data:${mime || "image/png"};base64,${data_b64}`);
    setStaged(null);
  };

  // Save the sticker SET (a list of base64 PNG cells) via card.stickers_save.
  const saveSet = async (cells: string[]) => {
    const r = await hubCall<{ urls?: string[] }>("card.stickers_save", { path: cardPath, data_b64: cells }, 30000);
    if (Array.isArray(r.urls)) setCurSet(r.urls.map((u) => assetUrl(String(u))));
    setStagedSet(null);
  };

  const downloadName = () => {
    if (staged) return `${kind}.${staged.ext || "png"}`;
    const m = curSrc.match(/^data:image\/(\w+)/);
    if (m) return `${kind}.${m[1] === "jpeg" ? "jpg" : m[1]}`;
    const ext = (curSrc.split("?")[0].split(".").pop() || "png").toLowerCase();
    return `${kind}.${ext.length <= 4 ? ext : "png"}`;
  };
  const download = () => {
    if (!previewSrc) return;
    const a = document.createElement("a");
    a.href = previewSrc;
    a.download = downloadName();
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // generate → stage a preview (the user confirms with 保存).
  const generate = async () => {
    const hasCurrent = isSet ? curSet.length > 0 : !!curSrc;
    const hasStaged = isSet ? !!stagedSet : !!staged;
    if (hasCurrent && !hasStaged && !confirm(t("vis-regen-overwrite"))) return;
    setWorking(t("vis-generating"));
    try {
      const out = await runVisualJob(
        hubCall,
        { path: cardPath, kind, brief: await getBrief(), refs: getRefs() },
        tick,
      );
      setIdle();
      if (isSet) {
        setStagedSet(out.stickers || []);
        setStagedNote(out.note || "");
      } else {
        setStaged({ data_b64: out.data_b64 || "", mime: out.mime || "image/png", ext: out.ext || "png", note: out.note });
      }
    } catch (e) {
      fail(e);
    }
  };

  // generateAndSave: used by "generate all" (auto-save, no per-asset confirm).
  const generateAndSave = async () => {
    setWorking(t("vis-generating"));
    try {
      const out = await runVisualJob(
        hubCall,
        { path: cardPath, kind, brief: await getBrief(), refs: getRefs() },
        tick,
      );
      if (isSet) await saveSet(out.stickers || []);
      else await saveBytes(out.data_b64 || "", out.mime || "image/png", out.ext || "png");
      setIdle();
    } catch (e) {
      fail(e);
      throw e;
    }
  };
  registerGenerate(generateAndSave);

  const saveStaged = async () => {
    if (isSet ? !stagedSet : !staged) return;
    setWorking(t("saving"));
    try {
      if (isSet) await saveSet(stagedSet || []);
      else if (staged) await saveBytes(staged.data_b64, staged.mime, staged.ext);
      setIdle();
      deckToast(t("saved"));
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };

  const onUpload = async (f: File) => {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    const exts = kind === "avatar" ? (AVATAR_EXTS as readonly string[]) : ART_EXTS;
    const cap = kind === "avatar" ? AVATAR_UPLOAD_MAX : ART_UPLOAD_MAX;
    if (!exts.includes(ext)) {
      setErrText(t("av-up-type"));
      return;
    }
    if (f.size > cap) {
      setErrText(t("av-up-size"));
      return;
    }
    setWorking(t("saving"));
    try {
      const b64 = await fileToB64(f);
      if (kind === "avatar") {
        const r = await hubCall<{ data_uri?: string }>("card.avatar_upload", { path: cardPath, data_b64: b64, ext }, 30000);
        if (r.data_uri) setCurSrc(r.data_uri);
      } else {
        const r = await hubCall<{ url?: string }>(
          "card.asset_save",
          { path: cardPath, kind, data_b64: b64, ext: ext === "jpg" ? "jpg" : ext },
          30000,
        );
        if (r.url) setCurSrc(assetUrl(String(r.url)));
        if (kind === "keyvisual") onAnchor(`data:${f.type || "image/png"};base64,${b64}`);
      }
      setStaged(null);
      setIdle();
      deckToast(t("saved"));
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };

  const onDelete = async () => {
    if (!confirm(t("vis-del-q"))) return;
    setWorking(t("vis-deleting"));
    try {
      await hubCall("card.asset_delete", { path: cardPath, kind }, 20000);
      setCurSrc("");
      setCurSet([]);
      setStaged(null);
      setStagedSet(null);
      if (kind === "keyvisual") onAnchor(null);
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };

  const hasAnyImage = isSet ? previewSet.length > 0 : !!previewSrc;
  const hasStaged = isSet ? !!stagedSet : !!staged;

  return (
    <div className="vis-slot">
      <div className="vis-slot-head">
        <b>{t(("vis-kind-" + kind) as Parameters<TFn>[0])}</b>
      </div>
      <div className="vis-slot-preview">
        {isSet ? (
          previewSet.length > 0 ? (
            <div className="vis-sticker-grid">
              {previewSet.map((src, i) => (
                <div className="vis-sticker-cell" key={i}>
                  <img src={src} alt="" />
                </div>
              ))}
            </div>
          ) : (
            <span className="vis-slot-empty">{t("vis-empty")}</span>
          )
        ) : previewSrc ? (
          <img src={previewSrc} alt="" />
        ) : (
          <span className="vis-slot-empty">{t("vis-empty")}</span>
        )}
      </div>
      <div className="vis-slot-acts">
        <button
          className="btn soft sm"
          disabled={disabled || busy || !canGenerate}
          title={canGenerate ? undefined : t("vis-need-key")}
          onClick={() => void generate()}
        >
          {t("vis-generate")}
        </button>
        {!isSet && (
          <button className="btn soft sm" disabled={disabled || busy} onClick={() => fileInput.current?.click()}>
            {t("av-upload")}
          </button>
        )}
        {!isSet && previewSrc && (
          <button className="btn text sm" disabled={busy} onClick={download}>
            {t("vis-download")}
          </button>
        )}
        {hasAnyImage && (
          <button className="btn text sm" disabled={disabled || busy} onClick={() => void onDelete()}>
            {t("del-word")}
          </button>
        )}
        <input
          ref={fileInput}
          type="file"
          accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files && e.target.files[0];
            e.target.value = "";
            if (f) void onUpload(f);
          }}
        />
      </div>
      {(busyMsg || errText) && (
        <div className={errText ? "av-note err" : busy ? "av-note thinking" : "av-note"}>{errText || busyMsg}</div>
      )}
      {hasStaged && (
        <div className="vis-stage">
          {(isSet ? stagedNote : staged?.note) && (
            <div className="av-note" style={{ marginBottom: 6 }}>{isSet ? stagedNote : staged?.note}</div>
          )}
          <div className="vis-stage-acts">
            <button className="btn primary sm" disabled={busy} onClick={() => void saveStaged()}>
              {t("save")}
            </button>
            <button className="btn soft sm" disabled={busy || !canGenerate} onClick={() => void generate()}>
              {t("vis-regen")}
            </button>
            <button className="btn text sm" disabled={busy} onClick={() => { setStaged(null); setStagedSet(null); }}>
              {t("cancel")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
