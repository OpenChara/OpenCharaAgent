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
import { useT, type TFn } from "../../i18n";
import { useHubApi, useHubState } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { rpcErrText } from "../../lib/status";
import { fileToB64 } from "../../lib/file";
import { deckToast } from "../ui/deckToast";
import { avatarSrc } from "./visual";
import type { DeckCard } from "./types";
import { VisualSlot } from "./VisualSlot";
import {
  BRIEF_FIELDS, DEFAULT_KINDS, REF_MAX, initUrlFor, optionsFor,
  type Brief, type Ref, type VisKind,
} from "./visualShared";

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

  // Reference images PERSIST in the card's asset library (assets/) — they're the "参考图"
  // the user uploads, and a tavern import drops its art here too. Load them on open so
  // they survive leaving the view and guide generation; add/remove write through to the
  // library. (Only the first REF_MAX images are used as references.)
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const r = await hub.call<{ assets?: { rel: string; kind: string }[] }>(
          "card.assets_list", { path: cardPath }, 20000);
        const imgs = (r.assets || []).filter((a) => a.kind === "image").slice(0, REF_MAX);
        const loaded: Ref[] = [];
        for (const a of imgs) {
          try {
            const f = await hub.call<{ data_uri?: string }>(
              "card.asset_file_read", { path: cardPath, rel: a.rel }, 20000);
            const m = /^data:([^;]+);base64,(.*)$/.exec(f.data_uri || "");
            if (m) loaded.push({ mime: m[1], data_b64: m[2], rel: a.rel });
          } catch { /* skip an unreadable asset */ }
        }
        if (live && loaded.length) setRefs(loaded);
      } catch { /* no assets / offline — empty tray */ }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cardPath]);

  const addRef = async (f: File) => {
    if (refs.length >= REF_MAX) return;
    try {
      const b64 = await fileToB64(f);
      const ext = f.type === "image/webp" ? "webp" : f.type === "image/jpeg" ? "jpg" : "png";
      const saved = await hub.call<{ rel?: string }>(
        "card.asset_file_upload", { path: cardPath, name: `reference.${ext}`, data_b64: b64, ext }, 30000);
      setRefs((cur) => (cur.length >= REF_MAX ? cur
        : [...cur, { data_b64: b64, mime: f.type || "image/png", rel: saved?.rel }]));
      void refresh();
    } catch {
      deckToast(t("av-up-read"), true);
    }
  };
  const removeRef = (i: number) => {
    const r = refs[i];
    // A persisted reference lives in the asset library — deleting it unlinks the file
    // (irreversible), so confirm first.
    if (r?.rel) {
      if (!confirm(t("vis-ref-del-q"))) return;
      void hub.call("card.asset_file_delete", { path: cardPath, rel: r.rel }, 20000).then(() => refresh()).catch(() => {});
    }
    setRefs((cur) => cur.filter((_, j) => j !== i));
  };

  const slotApi = useRef<Partial<Record<VisKind, (() => Promise<void>) | null>>>({});
  const [genAllBusy, setGenAllBusy] = useState(false);
  // master/detail: one kind shown at a time, picked via the segmented selector.
  const [sel, setSel] = useState<VisKind>(kinds[0]);
  // per-kind generating flag (the amber dot) — reported by each panel; panels stay
  // mounted (hidden) so a background generation keeps running when you switch kinds.
  const [busyKinds, setBusyKinds] = useState<Partial<Record<VisKind, boolean>>>({});
  const setKindBusy = (k: VisKind, b: boolean) =>
    setBusyKinds((cur) => (cur[k] === b ? cur : { ...cur, [k]: b }));
  const kindHasArt = (k: VisKind): boolean =>
    k === "avatar" ? !!avatarSrc(card)
    : k === "sprite" ? !!card.sprite_url
    : k === "keyvisual" ? !!card.keyvisual_url
    : k === "background" ? !!card.bg_url
    : (card.stickers_urls || []).length > 0;

  const genKinds = [...kinds];  // every visual kind is generatable
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
      // The keyvisual is the identity ANCHOR — it must finish + save FIRST so the others
      // reference it. After that, avatar/sprite/stickers/background generate CONCURRENTLY
      // (the backend runs each on its own job thread; card writes are atomic + per-path
      // locked, so parallel completions can't lose a gallery entry).
      const run = (k: VisKind) => (slotApi.current[k]?.() ?? Promise.resolve()).catch(() => {});
      for (const k of orderedGenKinds.filter((x) => x === "keyvisual")) await run(k);
      await Promise.all(orderedGenKinds.filter((x) => x !== "keyvisual").map(run));
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
  const recipeSummary = brief
    ? [brief.style, brief.palette, brief.appearance]
        .map((x) => String(x || "").trim()).filter(Boolean).join(" · ")
    : "";

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
          {/* the 配方/recipe bar — collapsed it's a one-line summary; expanding edits
              the fields. When empty it pulses to point the way (the generation gate). */}
          <div className={"vis-recipe" + (hasBrief ? "" : " need")}>
            <span className="vis-recipe-chev" onClick={!disabled ? toggleBrief : undefined}>
              {briefOpen ? "▾" : "▸"}
            </span>
            <span className="vis-recipe-sum" onClick={!disabled ? toggleBrief : undefined}>
              <b>{t("vis-brief-title")}</b>
              {hasBrief ? ` · ${recipeSummary}` : ` — ${t("vis-recipe-need")}`}
            </span>
            <button
              className={"btn sm " + (hasBrief ? "text" : "primary")}
              disabled={disabled || briefBusy}
              onClick={() => void loadBrief(true).catch(() => {})}
            >
              {briefBusy ? <span className="spin" /> : hasBrief ? t("vis-brief-rebuild") : t("vis-recipe-gen")}
            </button>
          </div>
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

      {/* kind selector — one detail panel at a time; the dot shows status:
          green = has art, amber-pulse = generating, grey = empty. */}
      <div className="vis-seg">
        {kinds.map((kind) => (
          <button
            key={kind}
            className={"vis-seg-btn" + (sel === kind ? " on" : "")}
            onClick={() => setSel(kind)}
          >
            {t(("vis-kind-" + kind) as Parameters<TFn>[0])}
            <span
              className={"vis-seg-dot" + (busyKinds[kind] ? " gen" : kindHasArt(kind) ? "" : " empty")}
            />
          </button>
        ))}
      </div>

      {/* Panels stay mounted (hidden unless selected) so a background generation keeps
          running and its dot stays amber when you browse another kind. */}
      {kinds.map((kind) => (
        <VisualSlot
          key={kind}
          active={sel === kind}
          kind={kind}
          cardName={String(card.name || "")}
          cardPath={cardPath}
          initUrl={initUrlFor(kind, card)}
          initOptions={optionsFor(kind, card)}
          initSet={kind === "stickers" ? (card.stickers_urls || []).map((u) => assetUrl(String(u))) : []}
          initSheets={kind === "stickers" ? (card.sticker_sheets_urls || []).map((u) => assetUrl(String(u))) : []}
          disabled={disabled}
          canGenerate={hasImageKey}
          hasBrief={hasBrief}
          getBrief={() => loadBrief(false)}
          getRefs={refData}
          hubCall={hub.call.bind(hub)}
          refreshHub={refresh}
          onChanged={onChanged}
          onGenerated={() => { if (kind === "keyvisual") setHasKeyvisual(true); }}
          onBusy={(b) => setKindBusy(kind, b)}
          onFixMatte={() => nav("#/settings")}
          t={t}
          registerGenerate={(fn) => {
            slotApi.current[kind] = fn;
          }}
        />
      ))}
    </div>
  );
}
