/* AvatarControls — the shared presentation editor (app.js buildAvatarControls
 * 2163), used by both the deck avatar editor and the create-flow visual section.
 * It edits a `work` model in place: { name, avatar_uri, avatar_svg,
 * pending_avatar, theme }. Preview, Upload (png/jpg/jpeg/svg → pending_avatar),
 * AI 生成 (card.avatar_generate → confirm/cancel staged SVG), and two theme-color
 * pickers applied live. No raw-SVG textarea.
 *
 * Binding UI rule: AI generate shows a "思考 Ns" ticking state and reverts on
 * failure (surfacing the error); upload/colors apply optimistically. */

import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import type { HubClient } from "../../rpc";
import { rpcErrText } from "../../lib/status";
import { glyphOf } from "../../lib/format";
import { dataUriSvg, themeStyle } from "../deck/visual";
import { avatarFileError, avatarMime, safeSvgForPreview, type PendingAvatar } from "./avatar";

/** The live presentation model the controls mutate (a stable ref's `.current`). */
export interface AvatarWork {
  name: string;
  avatar_uri: string;
  avatar_svg: string;
  pending_avatar: PendingAvatar | null;
  theme: { primary: string; secondary: string };
}

function previewSrc(work: AvatarWork): string {
  if (work.pending_avatar) return `data:${work.pending_avatar.mime};base64,${work.pending_avatar.data_b64}`;
  if (work.avatar_uri) return String(work.avatar_uri);
  if (safeSvgForPreview(work.avatar_svg)) return dataUriSvg(work.avatar_svg);
  return "";
}

export function AvatarControls({
  work,
  hub,
  cardPath,
  disabled = false,
  onChange,
}: {
  work: AvatarWork;
  hub: HubClient;
  /** Gives the generator the character's persona for context (app.js opts.cardPath). */
  cardPath?: string;
  disabled?: boolean;
  /** Called after any mutation so a parent can mirror `work` (e.g. onto a draft). */
  onChange?: () => void;
}) {
  const t = useT();
  const [, force] = useState(0);
  const refresh = () => {
    onChange?.();
    force((n) => n + 1);
  };

  const fileRef = useRef<HTMLInputElement>(null);
  const [upErr, setUpErr] = useState<string>("");

  // AI generate state: idle | thinking(seconds) | candidate(svg)
  const [aiDesc, setAiDesc] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [aiSecs, setAiSecs] = useState(0);
  const [aiErr, setAiErr] = useState("");
  const [cand, setCand] = useState<string | null>(null);
  const startedAt = useRef(0);

  useEffect(() => {
    if (!aiBusy) return;
    const id = setInterval(() => setAiSecs(Math.max(1, Math.round((Date.now() - startedAt.current) / 1000))), 1000);
    return () => clearInterval(id);
  }, [aiBusy]);

  const onFile = (file: File | undefined) => {
    setUpErr("");
    if (!file) return;
    const err = avatarFileError(file.name, file.size);
    if (err) {
      setUpErr(t(err));
      return;
    }
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = String(reader.result || "").split(",")[1] || "";
      work.pending_avatar = { data_b64: b64, ext, mime: avatarMime(ext) };
      work.avatar_uri = "";
      work.avatar_svg = "";
      refresh();
    };
    reader.onerror = () => setUpErr(t("av-up-read"));
    reader.readAsDataURL(file);
  };

  const generate = async () => {
    if (disabled) return;
    setAiErr("");
    setCand(null);
    setAiBusy(true);
    setAiSecs(1);
    startedAt.current = Date.now();
    try {
      const r = await hub.call<{ avatar_svg?: string }>(
        "card.avatar_generate",
        { card_path: cardPath || "", description: aiDesc.trim() },
        180000,
      );
      const svg = String((r && r.avatar_svg) || "");
      if (!safeSvgForPreview(svg)) {
        setAiErr(t("av-ai-bad"));
        return;
      }
      setCand(svg); // stage for confirm/cancel (do NOT touch work yet)
    } catch (e) {
      setAiErr(rpcErrText(t, e as { message?: string }));
    } finally {
      setAiBusy(false);
    }
  };

  const confirmCand = () => {
    if (!cand) return;
    work.avatar_svg = cand;
    work.avatar_uri = "";
    work.pending_avatar = null;
    setCand(null);
    refresh();
  };

  const setColor = (slot: "primary" | "secondary", value: string) => {
    work.theme[slot] = value.toUpperCase();
    refresh();
  };
  const clearSecondary = () => {
    work.theme.secondary = "";
    refresh();
  };

  const src = previewSrc(work);
  const primary = /^#[0-9a-fA-F]{6}$/.test(work.theme.primary) ? work.theme.primary.toUpperCase() : "#5B9FD4";
  const secondary = /^#[0-9a-fA-F]{6}$/.test(work.theme.secondary) ? work.theme.secondary.toUpperCase() : "#FFFFFF";

  return (
    <div className="av-controls">
      <div className="av-top">
        <div className="av-preview" style={themeStyle(work)}>
          {src ? <img src={src} alt="" /> : glyphOf(work.name)}
        </div>
        <div className="av-side">
          <div className="av-sec">
            <h4>{t("av-image")}</h4>
            <div className="av-row">
              <button className="btn soft" disabled={disabled} onClick={() => fileRef.current?.click()}>
                {t("av-upload")}
              </button>
            </div>
            {upErr && <div className="av-note err" style={{ marginTop: 6 }}>{upErr}</div>}
            <input
              ref={fileRef}
              type="file"
              accept=".png,.jpg,.jpeg,.svg,image/png,image/jpeg,image/svg+xml"
              style={{ display: "none" }}
              onChange={(e) => {
                onFile(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
          </div>
          <div className="av-sec">
            <h4>{t("av-ai")}</h4>
            <div className="av-ai-row">
              <input
                placeholder={t("av-ai-desc-ph")}
                value={aiDesc}
                disabled={disabled || aiBusy}
                onChange={(e) => setAiDesc(e.target.value)}
              />
              <button className="btn soft" disabled={disabled || aiBusy} onClick={() => void generate()}>
                {t("av-ai-go")}
              </button>
            </div>
            {aiBusy && <div className="av-note thinking" style={{ marginTop: 6 }}>{t("av-ai-thinking", { n: aiSecs })}</div>}
            {aiErr && <div className="av-note err" style={{ marginTop: 6 }}>{aiErr}</div>}
            {cand && (
              <>
                <div className="av-note" style={{ marginTop: 6 }}>{t("av-ai-confirm-q")}</div>
                <div className="av-ai-confirm">
                  <div className="av-cand" style={themeStyle(work)}>
                    <img src={dataUriSvg(cand)} alt="" />
                  </div>
                  <div className="av-ai-confirm-acts">
                    <button className="btn primary" onClick={confirmCand}>{t("av-ai-confirm-yes")}</button>
                    <button className="btn text" onClick={() => setCand(null)}>{t("av-ai-confirm-no")}</button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
      <div className="av-sec">
        <h4>{t("av-colors")}</h4>
        <div className="av-color-row">
          <label className="av-color">
            <span>{t("av-color-primary")}</span>
            <input type="color" value={primary} disabled={disabled} onChange={(e) => setColor("primary", e.target.value)} />
          </label>
          <label className="av-color">
            <span>{t("av-color-secondary")}</span>
            <input
              type="color"
              className={work.theme.secondary ? "" : "av-color-unset"}
              value={secondary}
              disabled={disabled}
              onChange={(e) => setColor("secondary", e.target.value)}
            />
            <button className="btn text tiny" title={t("av-color-clear")} disabled={disabled} onClick={clearSecondary}>
              ×
            </button>
          </label>
        </div>
      </div>
      {/* The avatar editor sets the face only; sprite/background come later in the
          deck card editor's 视觉 tab (R9). app.js:2446 visual-after-wake. */}
      <div className="av-note">{t("visual-after-wake")}</div>
    </div>
  );
}
