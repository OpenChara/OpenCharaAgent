/* VisualSlot — one art kind's detail panel (preview + candidate gallery + the
   generate / upload / 去背景 / delete actions). Extracted from VisualEditor; all
   data arrives via props, so it shares only the pure helpers in visualShared. */
import { useEffect, useRef, useState } from "react";
import { assetUrl } from "../../rpc";
import type { TFn } from "../../i18n";
import { rpcErrText } from "../../lib/status";
import { fileToB64 } from "../../lib/file";
import { deckToast } from "../ui/deckToast";
import { AVATAR_EXTS, AVATAR_UPLOAD_MAX } from "../overlays/avatar";
import {
  ART_EXTS, ART_UPLOAD_MAX, assetName, runVisualJob, stickerSlug,
  type Brief, type HubCall, type VisKind,
} from "./visualShared";

export function VisualSlot({
  active,
  kind,
  cardName,
  cardPath,
  initUrl,
  initOptions,
  initSet,
  initSheets,
  disabled,
  canGenerate,
  hasBrief,
  getBrief,
  getRefs,
  hubCall,
  refreshHub,
  onChanged,
  onGenerated,
  onBusy,
  onFixMatte,
  t,
  registerGenerate,
}: {
  active: boolean;
  kind: VisKind;
  cardName: string;
  cardPath: string;
  initUrl: string;
  initOptions: string[];
  initSet: string[];
  initSheets: string[];
  disabled: boolean;
  canGenerate: boolean;
  hasBrief: boolean;
  getBrief: () => Promise<Brief>;
  getRefs: () => string[];
  hubCall: HubCall;
  refreshHub: () => Promise<void>;
  onChanged: () => void;
  onGenerated: () => void;
  onBusy: (b: boolean) => void;
  onFixMatte: () => void;
  t: TFn;
  registerGenerate: (fn: () => Promise<void>) => void;
}) {
  const isSet = kind === "stickers";
  // avatar is a gallery kind too now (candidates + select); only stickers isn't single.
  const hasGallery = kind === "sprite" || kind === "keyvisual" || kind === "background" || kind === "avatar";
  const [curSrc, setCurSrc] = useState(initUrl);
  const [curSet, setCurSet] = useState<string[]>(initSet);
  const [sheets, setSheets] = useState<string[]>(initSheets);
  const [stickerGrid, setStickerGrid] = useState(3); // 1×1 / 2×2 / 3×3 at generation
  // The non-destructive candidate gallery (sprite/keyvisual/background): kept urls +
  // the selected filename. Selecting/removing/去背景 hit card.asset_* and update here.
  const [options, setOptions] = useState<string[]>(initOptions);
  const [selName, setSelName] = useState<string>(initUrl ? assetName(initUrl) : "");
  const [view, setView] = useState(0);     // sticker browse index in the big preview
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState("");
  const [errText, setErrText] = useState("");
  const [extra, setExtra] = useState("");  // optional 额外提示词 for this generation
  // A cut-kind (sprite/stickers) was generated but the background wasn't removed
  // (engine not ready). Generation SUCCEEDED — this is a soft, fixable notice.
  const [matteSkipped, setMatteSkipped] = useState(false);
  const wantsCut = isSet || kind === "sprite";
  const fileInput = useRef<HTMLInputElement>(null);
  // Generation queue: clicking 新生成 repeatedly enqueues more (each a kept candidate),
  // shown as spinning placeholders in the rail. genAll awaits `generate` directly.
  const qRef = useRef(0);
  const [qN, setQN] = useState(0);
  const drainingRef = useRef(false);

  // Cache-bust so a regenerated asset at the SAME url actually re-renders.
  const bust = (u: string) => (u && u.startsWith("/") ? `${u}${u.includes("?") ? "&" : "?"}v=${Date.now()}` : u);

  const setWorking = (msg: string) => { setBusy(true); setBusyMsg(msg); setErrText(""); };
  const setIdle = () => { setBusy(false); setBusyMsg(""); };
  const fail = (e: unknown) => { setBusy(false); setBusyMsg(""); setErrText(rpcErrText(t, e as { message?: string })); };

  // Report busy (real op OR a queued generation) up so the kind dot pulses amber even
  // when this (hidden) panel isn't the selected one. (ref so it's not a render dep.)
  const onBusyRef = useRef(onBusy);
  onBusyRef.current = onBusy;
  useEffect(() => { onBusyRef.current(busy || qN > 0); }, [busy, qN]);

  // generate → the BACKEND auto-saves; we just reflect the saved result + refresh. If
  // the user leaves mid-generation the job still finishes and the card still updates.
  const generate = async () => {
    // Every kind APPENDS a kept candidate (stickers, sprite/keyvisual/background, AND
    // avatar), so generation is always non-destructive — no overwrite confirm.
    setWorking(t("vis-generating"));
    try {
      const params: Record<string, unknown> = {
        path: cardPath, kind, brief: await getBrief(), refs: getRefs(), extra: extra.trim(),
      };
      if (isSet) params.grid = stickerGrid;
      const out = await runVisualJob(
        hubCall,
        params,
        (sec) => setBusyMsg(t("vis-gen-progress", { n: sec })),
      );
      if (isSet) {
        setCurSet((out.urls || []).map((u) => bust(assetUrl(String(u)))));
        if (out.sheet_urls) setSheets(out.sheet_urls.map((u) => assetUrl(String(u))));
      } else if (out.url) {
        // gallery kinds incl. avatar: show the new candidate + keep it in the rail
        const u = assetUrl(String(out.url));
        setCurSrc(kind === "avatar" && out.data_uri ? out.data_uri : bust(u));
        setSelName(assetName(u));
        if (out.options) setOptions(out.options.map((o) => assetUrl(String(o))));
        else setOptions((prev) => (prev.includes(u) ? prev : [...prev, u]));
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

  // Enqueue a generation (click 新生成 again to queue more); the drain runs them in turn.
  const drain = async () => {
    if (drainingRef.current) return;
    drainingRef.current = true;
    while (qRef.current > 0) {
      await generate().catch(() => {});
      qRef.current -= 1;
      setQN(qRef.current);
    }
    drainingRef.current = false;
  };
  const enqueueGen = () => {
    // Every kind APPENDS a kept candidate now (incl. avatar), so there's no overwrite to
    // confirm — just enqueue (click again to queue more).
    qRef.current += 1;
    setQN(qRef.current);
    void drain();
  };

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
        const r = await hubCall<{ data_uri?: string; url?: string; options?: string[] }>(
          "card.avatar_upload", { path: cardPath, data_b64: b64, ext }, 30000);
        if (r.data_uri) setCurSrc(r.data_uri);
        if (r.url) setSelName(assetName(assetUrl(String(r.url))));
        if (r.options) setOptions(r.options.map((o) => assetUrl(String(o))));  // keep the candidate rail
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

  // ── stickers: per-image rename / delete + raw-sheet re-slice ──────────────────
  const applySet = (r: { urls?: string[] }) =>
    setCurSet((r.urls || []).map((u) => bust(assetUrl(String(u)))));
  const renameSticker = async (url: string) => {
    const name = assetName(url);
    if (!name) return;
    const next = window.prompt(t("vis-rename"), stickerSlug(url));
    if (next == null || !next.trim()) return;
    setWorking(t("saving"));
    try {
      applySet(await hubCall("card.sticker_rename", { path: cardPath, old: name, new: next.trim() }, 15000));
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) { fail(e); }
  };
  const removeSticker = async (url: string) => {
    const name = assetName(url);
    if (!name) return;
    setWorking(t("vis-deleting"));
    try {
      applySet(await hubCall("card.sticker_remove", { path: cardPath, name }, 15000));
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) { fail(e); }
  };
  const resliceSheet = async (url: string) => {
    const name = assetName(url);
    if (!name) return;
    setWorking(t("vis-cutting"));
    try {
      applySet(await hubCall("card.sticker_reslice", { path: cardPath, sheet: name, grid: stickerGrid }, 120000));
      setIdle();
      await refreshHub();
      onChanged();
    } catch (e) { fail(e); }
  };

  const hasAnyImage = isSet ? curSet.length > 0 : !!curSrc;
  // The generate entry points QUEUE (busy doesn't block them — clicking again just
  // appends another spinning candidate), matching the rail's ＋ cell. Only the
  // key/brief/permission gates disable them.
  const genDisabled = disabled || !canGenerate || !hasBrief;
  // browse indices for the big preview
  const vi = curSet.length ? ((view % curSet.length) + curSet.length) % curSet.length : 0;
  const selIdx = options.findIndex((u) => assetName(u) === selName);
  const previewSrc = isSet ? (curSet[vi] || "") : curSrc;
  const count = isSet ? curSet.length : options.length;
  const browse = (d: number) => {
    if (isSet) { if (curSet.length) setView((v) => v + d); return; }
    if (!options.length) return;
    const i = selIdx < 0 ? 0 : selIdx;
    void selectCand(options[((i + d) % options.length + options.length) % options.length]);
  };

  return (
    <div className="vis-detail" style={active ? undefined : { display: "none" }}>
      <div className="vis-stage">
        <div className={"vis-preview" + (wantsCut ? " cut" : "")}>
          {previewSrc ? (
            <img src={previewSrc} alt="" />
          ) : (
            <span className="vis-preview-empty">{t("vis-empty")}</span>
          )}
          {count > 1 && (
            <>
              <button className="vis-arr l" onClick={() => browse(-1)} title="‹">‹</button>
              <button className="vis-arr r" onClick={() => browse(1)} title="›">›</button>
            </>
          )}
          {hasAnyImage && (
            <span className="vis-badge">
              {isSet
                ? `${stickerSlug(curSet[vi]) || "—"} · ${vi + 1}/${curSet.length}`
                : count > 1
                  ? `${(selIdx < 0 ? 0 : selIdx) + 1}/${count}`
                  : t(("vis-kind-" + kind) as Parameters<TFn>[0])}
            </span>
          )}
        </div>
        <div className="vis-acts">
          <button
            className="btn primary sm vis-gen-btn"
            disabled={genDisabled}
            title={!canGenerate ? t("vis-need-key") : !hasBrief ? t("vis-need-brief") : undefined}
            onClick={enqueueGen}
          >
            {hasAnyImage ? t("vis-generate-more") : t("vis-generate")}
          </button>
          {isSet && (
            <div className="vis-grid-pick" title={t("vis-grid-label")}>
              {[1, 2, 3].map((n) => (
                <button
                  key={n}
                  className={"vis-grid-btn" + (stickerGrid === n ? " on" : "")}
                  disabled={disabled}
                  onClick={() => setStickerGrid(n)}
                >
                  {n}×{n}
                </button>
              ))}
            </div>
          )}
          <input
            className="vis-extra"
            value={extra}
            disabled={disabled}
            placeholder={t("vis-extra-ph")}
            onChange={(e) => setExtra(e.target.value)}
          />
          {hasGallery && kind !== "avatar" && curSrc && (
            <button className="btn soft sm" disabled={disabled || busy} onClick={() => void doMatte()}>
              {t("vis-cut")}
            </button>
          )}
          {!isSet && (
            <button className="btn soft sm" disabled={disabled || busy} onClick={() => fileInput.current?.click()}>
              {t("av-upload")}
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
        {isSet && sheets.length > 0 && (
          <div className="vis-sheets">
            <span className="vis-sheets-label">{t("vis-sheet-label")}</span>
            <div className="vis-sheets-row">
              {sheets.map((u) => (
                <div className="vis-sheet" key={u}>
                  <img src={u} alt="" />
                  <button className="btn text sm" disabled={disabled || busy} onClick={() => void resliceSheet(u)}>
                    {t("vis-reslice")}
                  </button>
                </div>
              ))}
            </div>
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

      <div className="vis-rail">
        <h5>{t("vis-cand-title", { n: count })}</h5>
        <div className={"vis-rail-grid" + (isSet ? " sticker" : "")}>
          {isSet
            ? curSet.map((u, i) => (
                <div
                  key={u}
                  className={"vis-cand" + (wantsCut ? " cut" : "") + (i === vi ? " on" : "")}
                  onClick={() => setView(i)}
                >
                  <img src={u} alt="" />
                  {!disabled && (
                    <>
                      <span className="vis-cand-tag" title={t("vis-rename")}
                        onClick={(e) => { e.stopPropagation(); void renameSticker(u); }}>
                        {stickerSlug(u) || "—"}
                      </span>
                      <button className="vis-cand-x" title={t("del-word")} disabled={busy}
                        onClick={(e) => { e.stopPropagation(); void removeSticker(u); }}>×</button>
                    </>
                  )}
                </div>
              ))
            : options.map((u) => {
                const nm = assetName(u);
                return (
                  <div key={u} className={"vis-cand" + (wantsCut ? " cut" : "") + (nm === selName ? " on" : "")}
                    title={t("vis-cand-pick")} onClick={() => void selectCand(u)}>
                    <img src={u} alt="" />
                    {nm === selName && <span className="vis-cand-pick">✓</span>}
                    {!disabled && (
                      <button className="vis-cand-x" title={t("del-word")} disabled={busy}
                        onClick={(e) => { e.stopPropagation(); void removeCand(u); }}>×</button>
                    )}
                  </div>
                );
              })}
          {Array.from({ length: qN }, (_, i) => (
            <div key={"busy" + i} className="vis-cand busy"><span className="spin" /></div>
          ))}
          {!disabled && canGenerate && hasBrief && (
            <button className="vis-cand add" title={t("vis-generate-more")} onClick={enqueueGen}>＋</button>
          )}
        </div>
      </div>
    </div>
  );
}
