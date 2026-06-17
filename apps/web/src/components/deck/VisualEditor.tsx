/* VisualEditor — the R9 visual-set editor, a React port of app.js
 * buildVisualsControls (the deleted vanilla renderer). Per-kind
 * (avatar / sprite / background) generate via card.visual_brief → card.visual_generate
 * with a ticking "生成中…（约 10–30s）" state, regenerate, current art shown
 * (assetUrl-wrapped). One brief is built once and reused across the set so
 * "generate all" pays for ONE brief. Reference-image tray (≤3) guides generation.
 * Save / delete via card.avatar_upload (avatar) / card.asset_save + card.asset_delete.
 *
 * Each action writes to the card immediately (no separate Save button) — instant
 * feedback. Binding UI rule (CLAUDE.md): every generate/save shows a working state
 * (never freezes the long call), then reverts + surfaces errors via deckToast.
 * Builtin / locked / non-JSON cards are read-only (disabled).
 *
 * Embedded inside the open CardEditor modal — it never closes the editor; after a
 * save it refreshes the hub snapshot (live deck cards) so the new art shows. */

import { useRef, useState } from "react";
import { assetUrl } from "../../rpc";
import { useT, type TFn } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";
import { AVATAR_EXTS, AVATAR_UPLOAD_MAX } from "../overlays/avatar";
import { avatarSrc } from "./visual";
import type { DeckCard } from "./types";

const ART_EXTS = ["png", "jpg", "jpeg", "webp"];
const ART_UPLOAD_MAX = 16 * 1024 * 1024;
const VIS_KINDS = ["avatar", "sprite", "background"] as const;
const REF_MAX = 3; // user reference images per generation

type VisKind = (typeof VIS_KINDS)[number];

interface Ref {
  data_b64: string;
  mime: string;
}

interface Brief {
  appearance?: string;
  palette?: string;
  world?: string;
  theme?: string;
  [k: string]: unknown;
}

interface GenResult {
  data_b64: string;
  mime?: string;
  ext?: string;
  kind?: string;
  matted?: boolean;
  note?: string;
  brief?: Brief;
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
   every hub.state, so it must stay small. (app.js downscalePngB64.) */
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

function initUrlFor(kind: VisKind, card: DeckCard): string {
  if (kind === "avatar") return avatarSrc(card);
  if (kind === "sprite") return card.sprite_url ? assetUrl(String(card.sprite_url)) : "";
  return card.bg_url ? assetUrl(String(card.bg_url)) : "";
}

export function VisualEditor({
  cardPath,
  card,
  disabled,
  onChanged,
}: {
  cardPath: string;
  card: DeckCard;
  disabled: boolean;
  onChanged: () => void;
}) {
  const t = useT();
  const { hub, refresh } = useHubApi();
  const [refs, setRefs] = useState<Ref[]>([]);
  // The brief is built once via card.visual_brief and reused across the set so
  // "generate all" pays for ONE brief, not one per asset.
  const cachedBrief = useRef<Brief | null>(null);
  const refInput = useRef<HTMLInputElement>(null);

  const refData = () => refs.map((r) => `data:${r.mime};base64,${r.data_b64}`);
  const getBrief = async (): Promise<Brief> => {
    if (!cachedBrief.current) {
      const r = await hub.call<{ brief?: Brief }>("card.visual_brief", { path: cardPath }, 180000);
      cachedBrief.current = (r && r.brief) || {};
    }
    return cachedBrief.current;
  };

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
  const slotApi = useRef<Record<VisKind, (() => Promise<void>) | null>>({
    avatar: null,
    sprite: null,
    background: null,
  });
  const [genAllBusy, setGenAllBusy] = useState(false);

  const generateAll = async () => {
    if (!confirm(t("vis-gen-all-confirm", { n: VIS_KINDS.length }))) return;
    setGenAllBusy(true);
    cachedBrief.current = null; // a fresh set → a fresh brief
    try {
      for (const k of VIS_KINDS) {
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
        {VIS_KINDS.map((kind) => (
          <VisualSlot
            key={kind}
            kind={kind}
            cardPath={cardPath}
            initUrl={initUrlFor(kind, card)}
            disabled={disabled}
            getBrief={getBrief}
            refData={refData}
            hubCall={hub.call.bind(hub)}
            refreshHub={refresh}
            onChanged={onChanged}
            t={t}
            registerGenerate={(fn) => {
              slotApi.current[kind] = fn;
            }}
          />
        ))}
      </div>

      <div className="vis-all">
        <button className="btn primary" disabled={disabled || genAllBusy} onClick={() => void generateAll()}>
          {genAllBusy ? <span className="spin" /> : t("vis-gen-all", { n: VIS_KINDS.length })}
        </button>
        <div className="av-note" style={{ marginTop: 6 }}>
          {t("vis-gen-all-cost", { n: VIS_KINDS.length })}
        </div>
      </div>
    </div>
  );
}

type HubCall = <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>;

function VisualSlot({
  kind,
  cardPath,
  initUrl,
  disabled,
  getBrief,
  refData,
  hubCall,
  refreshHub,
  onChanged,
  t,
  registerGenerate,
}: {
  kind: VisKind;
  cardPath: string;
  initUrl: string;
  disabled: boolean;
  getBrief: () => Promise<Brief>;
  refData: () => string[];
  hubCall: HubCall;
  refreshHub: () => Promise<void>;
  onChanged: () => void;
  t: TFn;
  registerGenerate: (fn: () => Promise<void>) => void;
}) {
  const [curSrc, setCurSrc] = useState(initUrl);
  // A generated-but-unsaved result staged for 保存 / 重新生成 / 取消.
  const [staged, setStaged] = useState<{ data_b64: string; mime: string; ext: string; note?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState("");
  const [errText, setErrText] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const previewSrc = staged ? `data:${staged.mime};base64,${staged.data_b64}` : curSrc;

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

  // Save staged/uploaded bytes: avatar → downscaled PNG via avatar_upload (it's
  // inlined into hub.state); sprite/background → asset_save in the true format.
  const saveBytes = async (data_b64: string, mime: string, ext: string) => {
    if (kind === "avatar") {
      const small = await downscalePngB64(`data:${mime || "image/png"};base64,${data_b64}`, 512);
      const r = await hubCall<{ data_uri?: string }>("card.avatar_upload", { path: cardPath, data_b64: small, ext: "png" }, 30000);
      if (r.data_uri) setCurSrc(r.data_uri);
    } else {
      const r = await hubCall<{ url?: string }>("card.asset_save", { path: cardPath, kind, data_b64, ext: ext || "png" }, 30000);
      if (r.url) setCurSrc(assetUrl(String(r.url)));
    }
    setStaged(null);
  };

  // generate → stage a preview (the user confirms with 保存).
  const generate = async () => {
    setWorking(t("vis-generating"));
    try {
      const out = await hubCall<GenResult>(
        "card.visual_generate",
        { path: cardPath, kind, brief: await getBrief(), refs: refData() },
        240000,
      );
      setIdle();
      setStaged({ data_b64: out.data_b64, mime: out.mime || "image/png", ext: out.ext || "png", note: out.note });
    } catch (e) {
      fail(e);
    }
  };

  // generateAndSave: used by "generate all" (auto-save, no per-asset confirm).
  const generateAndSave = async () => {
    setWorking(t("vis-generating"));
    try {
      const out = await hubCall<GenResult>(
        "card.visual_generate",
        { path: cardPath, kind, brief: await getBrief(), refs: refData() },
        240000,
      );
      await saveBytes(out.data_b64, out.mime || "image/png", out.ext || "png");
      setIdle();
    } catch (e) {
      fail(e);
      throw e;
    }
  };
  registerGenerate(generateAndSave);

  const saveStaged = async () => {
    if (!staged) return;
    setWorking(t("saving"));
    try {
      await saveBytes(staged.data_b64, staged.mime, staged.ext);
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
      setStaged(null);
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) {
      fail(e);
    }
  };

  return (
    <div className="vis-slot">
      <div className="vis-slot-head">
        <b>{t(("vis-kind-" + kind) as Parameters<TFn>[0])}</b>
      </div>
      <div className="vis-slot-preview">
        {previewSrc ? <img src={previewSrc} alt="" /> : <span className="vis-slot-empty">{t("vis-empty")}</span>}
      </div>
      <div className="vis-slot-acts">
        <button className="btn soft sm" disabled={disabled || busy} onClick={() => void generate()}>
          {t("vis-generate")}
        </button>
        <button className="btn soft sm" disabled={disabled || busy} onClick={() => fileInput.current?.click()}>
          {t("av-upload")}
        </button>
        <button className="btn text sm" disabled={disabled || busy} onClick={() => void onDelete()}>
          {t("del-word")}
        </button>
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
      {staged && (
        <div className="vis-stage">
          {staged.note && <div className="av-note" style={{ marginBottom: 6 }}>{staged.note}</div>}
          <div className="vis-stage-acts">
            <button className="btn primary sm" disabled={busy} onClick={() => void saveStaged()}>
              {t("save")}
            </button>
            <button className="btn soft sm" disabled={busy} onClick={() => void generate()}>
              {t("vis-regen")}
            </button>
            <button className="btn text sm" disabled={busy} onClick={() => setStaged(null)}>
              {t("cancel")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
