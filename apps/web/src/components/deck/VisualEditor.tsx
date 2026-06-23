/* VisualEditor — the R9 visual-set editor.
 *
 * Robust async model: generation is SLOW and must survive the user leaving the view,
 * so the BACKEND auto-saves. card.visual_generate kicks a job (returns a job_id) that
 * generates AND writes the result straight to the card; the client polls card.visual_job
 * only for progress and refreshes when it lands. If you switch tabs or close the card
 * mid-generation, the job still finishes and the card still updates — reopen to see it.
 *
 * The image BRIEF is persisted on the card (extensions.lunamoth.visual_brief): viewing
 * it returns the stored one (no re-pay), edits are saved via card.visual_brief_save, and
 * 重新生成 forces a rebuild. Identity-lock (anchor = the saved keyvisual) is applied
 * server-side, so the other kinds stay the same character without client bookkeeping.
 *
 * Per kind: 生成 (auto-saves), 上传 (manual), 下载, 删除. Builtin/locked/PNG cards are
 * read-only (controls disabled / read tiles). */

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
const DEFAULT_KINDS = ["keyvisual", "avatar", "sprite", "stickers", "background"] as const;
const GENERATABLE: Record<string, boolean> = {
  keyvisual: true, avatar: true, sprite: true, stickers: true, background: true,
};
const REF_MAX = 3; // user reference images per generation

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

const BRIEF_FIELDS: { k: string; labelKey: TKey; long?: boolean }[] = [
  { k: "appearance", labelKey: "vis-brief-appearance", long: true },
  { k: "style", labelKey: "vis-brief-style", long: true },
  { k: "palette", labelKey: "vis-brief-palette" },
  { k: "world", labelKey: "vis-brief-world", long: true },
  { k: "theme", labelKey: "vis-brief-theme" },
];

/* card.visual_job's ready payload — the backend already saved the result, so this
   carries the saved location, not raw bytes. */
interface GenResult {
  status?: string;
  saved?: boolean;
  kind?: string;
  url?: string;        // sprite/background/keyvisual asset url
  urls?: string[];     // stickers (the saved set)
  data_uri?: string;   // avatar (inlined)
  note?: string;
  matted?: boolean;    // false ⇒ a cut was wanted but skipped (engine not ready)
}

type HubCall = <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/* Kick card.visual_generate (returns a job_id) then poll card.visual_job until ready.
   A real failure surfaces as a rejected hubCall; "unknown" means the job expired. */
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
    const st = await hubCall<GenResult>("card.visual_job", { job_id: jobId }, 30000);
    if (st.status === "ready") return st;
    if (st.status === "unknown") throw new Error("the generation job expired — please try again");
  }
  throw new Error("generation timed out");
}

function fileToB64(f: File): Promise<string> {
  return new Promise((res, rej) => {
    const reader = new FileReader();
    reader.onload = () => res(String(reader.result || "").split(",")[1] || "");
    reader.onerror = () => rej(new Error("read failed"));
    reader.readAsDataURL(f);
  });
}

function initUrlFor(kind: VisKind, card: DeckCard): string {
  if (kind === "avatar") return avatarSrc(card);
  if (kind === "sprite") return card.sprite_url ? assetUrl(String(card.sprite_url)) : "";
  if (kind === "keyvisual") return card.keyvisual_url ? assetUrl(String(card.keyvisual_url)) : "";
  return card.bg_url ? assetUrl(String(card.bg_url)) : "";
}

// The candidate gallery urls for a kind (sprite/keyvisual/background keep a gallery;
// avatar + stickers don't use the single-image strip).
function optionsFor(kind: VisKind, card: DeckCard): string[] {
  const raw =
    kind === "sprite" ? card.sprite_options
    : kind === "keyvisual" ? card.keyvisual_options
    : kind === "background" ? card.bg_options
    : undefined;
  return Array.isArray(raw) ? raw.map((u) => assetUrl(String(u))) : [];
}

/* The candidate FILENAME from an /asset?p=<abspath> url — what asset_select/remove want. */
function assetName(url: string): string {
  try {
    const p = new URLSearchParams(url.split("?")[1] || "").get("p") || "";
    return decodeURIComponent(p).split("/").pop() || "";
  } catch {
    return "";
  }
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
  // Whether a keyvisual exists yet (drives the "generate the anchor first" hint).
  const [hasKeyvisual, setHasKeyvisual] = useState(!!card.keyvisual_url);

  const refData = () => refs.map((r) => `data:${r.mime};base64,${r.data_b64}`);

  // Persist brief edits on the card (debounced) so they survive leaving the view —
  // viewing the brief never re-pays the LLM.
  const briefSaveTimer = useRef<number | null>(null);
  const persistBrief = (b: Brief) => {
    if (briefSaveTimer.current) window.clearTimeout(briefSaveTimer.current);
    briefSaveTimer.current = window.setTimeout(() => {
      void hub.call("card.visual_brief_save", { path: cardPath, brief: b }, 15000).catch(() => {});
    }, 700);
  };

  // Load the brief — returns the STORED brief (no LLM) unless force rebuilds it.
  const loadBrief = async (force: boolean): Promise<Brief> => {
    if (cachedBrief.current && !force) {
      setBrief(cachedBrief.current);
      return cachedBrief.current;
    }
    setBriefBusy(true);
    setBriefErr("");
    try {
      const r = await hub.call<{ brief?: Brief }>("card.visual_brief", { path: cardPath, force }, 180000);
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
  const toggleBrief = () => {
    setBriefOpen((v) => !v);
    if (!cachedBrief.current && !briefBusy) void loadBrief(false).catch(() => {});
  };
  const editBrief = (k: string, v: string) => {
    const next: Brief = { ...(cachedBrief.current || {}), [k]: v };
    cachedBrief.current = next;
    setBrief(next);
    persistBrief(next);
  };

  // Background-removal readiness — relevant when a sprite/stickers cutout is wanted.
  const wantsCut = kinds.includes("sprite") || kinds.includes("stickers");
  const [matteReady, setMatteReady] = useState<boolean | null>(null);
  useEffect(() => {
    if (!wantsCut || !hasImageKey) return;
    let live = true;
    void hub
      .call<{ deps?: boolean; models?: { installed?: boolean }[] }>("matte.status", {}, 15000)
      // "ready" = a cutout model is INSTALLED (downloaded). We don't gate the hint on
      // deps_available() — that can read false until the freshly-installed deps import
      // (a process restart), and the user has clearly done their part once a model is
      // downloaded; a deps gap just falls back to an un-matted result with a note.
      .then((s) => { if (live) setMatteReady((s.models || []).some((m) => !!m.installed)); })
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

  const slotApi = useRef<Partial<Record<VisKind, (() => Promise<void>) | null>>>({});
  const [genAllBusy, setGenAllBusy] = useState(false);

  const genKinds = kinds.filter((k) => GENERATABLE[k]);
  // Anchor (keyvisual) first so the rest reference it (the backend reads the saved one).
  const orderedGenKinds = [
    ...genKinds.filter((k) => k === "keyvisual"),
    ...genKinds.filter((k) => k !== "keyvisual"),
  ];
  const showAnchorHint = hasImageKey && !disabled && kinds.includes("keyvisual") && !hasKeyvisual;

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
      await refresh();
      onChanged();
    } finally {
      setGenAllBusy(false);
    }
  };

  // A per-slot generate needs the shared image brief first (the description every
  // image is drawn from). Until it exists, the per-slot 生成 buttons are disabled
  // with a "write the brief first" hint; 一键全部生成 builds the brief itself, so
  // it stays enabled.
  const hasBrief = !!(brief && String(brief.appearance || "").trim());

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

      {showAnchorHint && (
        <div className="vis-invite">
          <div className="vis-invite-text">{t("vis-anchor-hint")}</div>
        </div>
      )}

      {wantsCut && hasImageKey && matteReady === false && !disabled && (
        <div className="vis-invite">
          <div className="vis-invite-text">{t("vis-matte-hint")}</div>
          <button className="btn soft sm" onClick={() => nav("#/settings")}>
            {t("vis-matte-cta")}
          </button>
        </div>
      )}

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

      {/* One prominent action right under the brief: write the brief AND generate
          every image in one go. (Moved up from the bottom of the editor.) */}
      {hasImageKey && orderedGenKinds.length > 0 && (
        <div className="vis-genall">
          <button
            className="btn primary vis-genall-btn"
            disabled={disabled || genAllBusy}
            onClick={() => void generateAll()}
          >
            {genAllBusy ? <span className="spin" /> : t("vis-gen-all", { n: orderedGenKinds.length })}
          </button>
          <div className="vis-genall-note">{t("vis-gen-all-cost", { n: orderedGenKinds.length })}</div>
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

      {hasImageKey && !hasBrief && !disabled && (
        <div className="av-note vis-need-brief-note">{t("vis-need-brief")}</div>
      )}
      <div className="vis-slots">
        {kinds.map((kind) => (
          <VisualSlot
            key={kind}
            kind={kind}
            cardName={String(card.name || "")}
            cardPath={cardPath}
            initUrl={initUrlFor(kind, card)}
            initOptions={optionsFor(kind, card)}
            initSet={kind === "stickers" ? (card.stickers_urls || []).map((u) => assetUrl(String(u))) : []}
            disabled={disabled}
            canGenerate={hasImageKey && !!GENERATABLE[kind]}
            hasBrief={hasBrief}
            getBrief={() => loadBrief(false)}
            getRefs={refData}
            hubCall={hub.call.bind(hub)}
            refreshHub={refresh}
            onChanged={onChanged}
            onGenerated={() => { if (kind === "keyvisual") setHasKeyvisual(true); }}
            onFixMatte={() => nav("#/settings")}
            t={t}
            registerGenerate={(fn) => {
              slotApi.current[kind] = fn;
            }}
          />
        ))}
      </div>

    </div>
  );
}

function VisualSlot({
  kind,
  cardName,
  cardPath,
  initUrl,
  initOptions,
  initSet,
  disabled,
  canGenerate,
  hasBrief,
  getBrief,
  getRefs,
  hubCall,
  refreshHub,
  onChanged,
  onGenerated,
  onFixMatte,
  t,
  registerGenerate,
}: {
  kind: VisKind;
  cardName: string;
  cardPath: string;
  initUrl: string;
  initOptions: string[];
  initSet: string[];
  disabled: boolean;
  canGenerate: boolean;
  hasBrief: boolean;
  getBrief: () => Promise<Brief>;
  getRefs: () => string[];
  hubCall: HubCall;
  refreshHub: () => Promise<void>;
  onChanged: () => void;
  onGenerated: () => void;
  onFixMatte: () => void;
  t: TFn;
  registerGenerate: (fn: () => Promise<void>) => void;
}) {
  const isSet = kind === "stickers";
  const hasGallery = kind === "sprite" || kind === "keyvisual" || kind === "background";
  const [curSrc, setCurSrc] = useState(initUrl);
  const [curSet, setCurSet] = useState<string[]>(initSet);
  // The non-destructive candidate gallery (sprite/keyvisual/background): kept urls +
  // the selected filename. Selecting/removing/去背景 hit card.asset_* and update here.
  const [options, setOptions] = useState<string[]>(initOptions);
  const [selName, setSelName] = useState<string>(initUrl ? assetName(initUrl) : "");
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState("");
  const [errText, setErrText] = useState("");
  const [extra, setExtra] = useState("");  // optional 额外提示词 for this generation
  // A cut-kind (sprite/stickers) was generated but the background wasn't removed
  // (engine not ready). Generation SUCCEEDED — this is a soft, fixable notice.
  const [matteSkipped, setMatteSkipped] = useState(false);
  const wantsCut = isSet || kind === "sprite";
  const fileInput = useRef<HTMLInputElement>(null);

  // Cache-bust so a regenerated asset at the SAME url actually re-renders.
  const bust = (u: string) => (u && u.startsWith("/") ? `${u}${u.includes("?") ? "&" : "?"}v=${Date.now()}` : u);

  const setWorking = (msg: string) => { setBusy(true); setBusyMsg(msg); setErrText(""); };
  const setIdle = () => { setBusy(false); setBusyMsg(""); };
  const fail = (e: unknown) => { setBusy(false); setBusyMsg(""); setErrText(rpcErrText(t, e as { message?: string })); };

  // generate → the BACKEND auto-saves; we just reflect the saved result + refresh. If
  // the user leaves mid-generation the job still finishes and the card still updates.
  const generate = async () => {
    const hasCurrent = isSet ? curSet.length > 0 : !!curSrc;
    if (hasCurrent && !confirm(t("vis-regen-overwrite"))) return;
    setWorking(t("vis-generating"));
    try {
      const out = await runVisualJob(
        hubCall,
        { path: cardPath, kind, brief: await getBrief(), refs: getRefs(), extra: extra.trim() },
        (sec) => setBusyMsg(t("vis-gen-progress", { n: sec })),
      );
      if (isSet) setCurSet((out.urls || []).map((u) => bust(assetUrl(String(u)))));
      else if (kind === "avatar") setCurSrc(out.data_uri || "");
      else if (out.url) {
        const u = assetUrl(String(out.url));
        setCurSrc(bust(u));
        setSelName(assetName(u));
        setOptions((prev) => (prev.includes(u) ? prev : [...prev, u]));  // keep the candidate
      }
      // Cut wanted but skipped → flag it (engine not ready); the image is still saved.
      setMatteSkipped(wantsCut && out.matted === false);
      setIdle();
      onGenerated();
      deckToast(t("vis-gen-done", {
        name: cardName || t("vis-gen-done-fallback"),
        kind: t(("vis-kind-" + kind) as Parameters<TFn>[0]),
      }));
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
      throw e;
    }
  };
  registerGenerate(generate);

  const onUpload = async (f: File) => {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    const exts = kind === "avatar" ? (AVATAR_EXTS as readonly string[]) : ART_EXTS;
    const cap = kind === "avatar" ? AVATAR_UPLOAD_MAX : ART_UPLOAD_MAX;
    if (!exts.includes(ext)) { setErrText(t("av-up-type")); return; }
    if (f.size > cap) { setErrText(t("av-up-size")); return; }
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
        if (r.url) setCurSrc(bust(assetUrl(String(r.url))));
        if (kind === "keyvisual") onGenerated();
      }
      setMatteSkipped(false); // a user upload — matte state unknown, don't warn
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
      setMatteSkipped(false);
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };

  const download = () => {
    if (!curSrc) return;
    const a = document.createElement("a");
    a.href = curSrc;
    const m = curSrc.match(/^data:image\/(\w+)/);
    a.download = `${kind}.${m ? (m[1] === "jpeg" ? "jpg" : m[1]) : "png"}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // ── candidate gallery (sprite/keyvisual/background) — non-destructive ──────────
  const selectCand = async (url: string) => {
    const name = assetName(url);
    if (!name || name === selName) return;
    setWorking(t("saving"));
    try {
      await hubCall("card.asset_select", { path: cardPath, kind, name }, 15000);
      setCurSrc(bust(url));
      setSelName(name);
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };
  const removeCand = async (url: string) => {
    const name = assetName(url);
    if (!name) return;
    setWorking(t("vis-deleting"));
    try {
      const r = await hubCall<{ selected?: string; options?: string[] }>(
        "card.asset_remove", { path: cardPath, kind, name }, 15000);
      const opts = (r.options || []).map((u) => assetUrl(String(u)));
      setOptions(opts);
      const sel = r.selected || "";
      setSelName(sel);
      const selUrl = opts.find((u) => assetName(u) === sel) || "";
      setCurSrc(selUrl ? bust(selUrl) : "");
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };
  const doMatte = async () => {
    setWorking(t("vis-cutting"));
    try {
      const r = await hubCall<{ url?: string; options?: string[] }>(
        "card.asset_matte", { path: cardPath, kind }, 60000);
      const opts = (r.options || []).map((u) => assetUrl(String(u)));
      if (opts.length) setOptions(opts);
      if (r.url) {
        const u = assetUrl(String(r.url));
        setCurSrc(bust(u));
        setSelName(assetName(u));
      }
      setMatteSkipped(false);
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };

  const hasAnyImage = isSet ? curSet.length > 0 : !!curSrc;

  return (
    <div className="vis-slot">
      <div className="vis-slot-head">
        <b>{t(("vis-kind-" + kind) as Parameters<TFn>[0])}</b>
      </div>
      {/* cut-kinds (sprite/stickers) want a transparent cut — a checkerboard makes
          the result self-evident: a real cut floats on it, a white-bg fill covers it. */}
      <div className={"vis-slot-preview" + (wantsCut ? " vis-checker" : "")}>
        {isSet ? (
          curSet.length > 0 ? (
            <div className="vis-sticker-grid">
              {curSet.map((src, i) => (
                <div className="vis-sticker-cell" key={i}>
                  <img src={src} alt="" />
                </div>
              ))}
            </div>
          ) : (
            <span className="vis-slot-empty">{t("vis-empty")}</span>
          )
        ) : curSrc ? (
          <img src={curSrc} alt="" />
        ) : (
          <span className="vis-slot-empty">{t("vis-empty")}</span>
        )}
      </div>
      <div className="vis-slot-acts">
        <button
          className="btn soft sm"
          disabled={disabled || busy || !canGenerate || !hasBrief}
          title={!canGenerate ? t("vis-need-key") : !hasBrief ? t("vis-need-brief") : undefined}
          onClick={() => void generate().catch(() => {})}
        >
          {t("vis-generate")}
        </button>
        <input
          className="vis-extra"
          value={extra}
          disabled={disabled || busy}
          placeholder={t("vis-extra-ph")}
          onChange={(e) => setExtra(e.target.value)}
        />
        {!isSet && (
          <button className="btn soft sm" disabled={disabled || busy} onClick={() => fileInput.current?.click()}>
            {t("av-upload")}
          </button>
        )}
        {hasGallery && curSrc && (
          <button className="btn text sm" disabled={disabled || busy} onClick={() => void doMatte()}>
            {t("vis-cut")}
          </button>
        )}
        {!isSet && curSrc && (
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
      {hasGallery && options.length > 0 && (
        <div className="vis-cands">
          {options.map((u) => {
            const nm = assetName(u);
            return (
              <div key={u} className={"vis-cand" + (nm === selName ? " on" : "")} title={t("vis-cand-pick")}>
                <img src={u} alt="" onClick={() => void selectCand(u)} />
                <button className="vis-cand-x" title={t("del-word")} onClick={() => void removeCand(u)}>×</button>
              </div>
            );
          })}
        </div>
      )}
      {(busyMsg || errText) && (
        <div className={errText ? "av-note err" : busy ? "av-note thinking" : "av-note"}>{errText || busyMsg}</div>
      )}
      {matteSkipped && !busy && !errText && (
        <div className="vis-matte-skip">
          <span className="vis-matte-skip-text">{t("vis-matte-skipped")}</span>
          <button className="btn soft sm" onClick={onFixMatte}>{t("go-settings")}</button>
        </div>
      )}
    </div>
  );
}
