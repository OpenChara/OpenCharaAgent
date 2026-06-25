/* ModelPane — Settings 模型 pane, after Hermes' Model pane: a PROVIDER box + a
 * MODEL box (provider:model) for the MAIN model, a Test-connection button, a
 * Reasoning control (OpenRouter-only — OpenRouter wraps the unified reasoning
 * param), then an "other modalities" section that mirrors Hermes' auxiliary
 * models: each modality defaults to the main model and can be overridden with its
 * own model. We surface Vision (image understanding) + Image-gen. Image-gen is
 * wired (image_model); the vision override is stored (vision_model) but its
 * routing pipeline is a separate follow-up.
 *
 * Providers/keys are added in the Providers pane; this pane only chooses models.
 * Provider switch → defaults.use_key; everything else → defaults.set. Instant. */

import { useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { errText, rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";
import { Select, type SelectOption } from "./Select";
import { Segmented } from "../ui/Segmented";
import { TaskModels, type ImageProvider } from "./TaskModels";
import { MatteSection } from "./MattePane";
import type { TKey } from "../../i18n";
import type { ModelInfo } from "../deck/types";

interface Defaults {
  provider?: string;
  base_url?: string;
  model?: string;
  reasoning?: string;
  image_provider?: string;
  image_model?: string;
  vision_model?: string;
  model_context?: number | string;
  model_refresh_interval?: number | string;
  has_key?: boolean;
}
interface KeyRow { label: string; provider: string; base_url: string; model: string; has_key: boolean; active: boolean }
interface TestResult { ok?: boolean; error?: { kind?: string }; capabilities?: { tools?: boolean; vision?: boolean } }

/* The pair we OFFER as quick picks. */
const RECOMMENDED = [
  { id: "deepseek/deepseek-v4-flash", note: "model-note-flash" },
  { id: "deepseek/deepseek-v4-pro", note: "model-note-pro" },
];
const REASONING = ["off", "low", "medium", "high"] as const;
const providerOf = (id: string) => (id.includes("/") ? id.split("/")[0] : "other").replace(/^[~@]/, "");

export function ModelPane() {
  const t = useT();
  const { hub, snapshot, refresh } = useHub();
  const nav = useNavigate();
  const defaults = (snapshot?.defaults as Defaults) || {};

  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsStale, setModelsStale] = useState(false);
  const [imageCatalog, setImageCatalog] = useState<ImageProvider[]>([]);
  const [busy, setBusy] = useState(false);
  const [savedTick, setSavedTick] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<TestResult | null>(null);
  const [ctx, setCtx] = useState(String(defaults.model_context || ""));
  useEffect(() => { setCtx(String(defaults.model_context || "")); }, [defaults.model_context]);
  // Model-list refresh interval, shown in DAYS (stored as seconds; blank = the 1-day default).
  const [refreshDays, setRefreshDays] = useState("");
  useEffect(() => {
    const secs = Number(defaults.model_refresh_interval || 0);
    setRefreshDays(secs > 0 ? String(+(secs / 86400).toFixed(2)) : "");
  }, [defaults.model_refresh_interval]);

  useEffect(() => {
    let on = true;
    hub.call<KeyRow[]>("keys.list", {}, 15000).then((k) => on && setKeys(Array.isArray(k) ? k : [])).catch(() => {});
    return () => { on = false; };
  }, [hub]);
  useEffect(() => {
    let on = true;
    hub.call<{ models?: ModelInfo[]; stale?: boolean }>("models.list", {}, 30000)
      .then((r) => { if (!on) return; setModels(Array.isArray(r?.models) ? r.models : []); setModelsStale(!!r?.stale); })
      .catch(() => {});
    return () => { on = false; };
  }, [hub, defaults.base_url]);
  // image-gen provider catalogue (providers + their models + key presence).
  // Refetch when the active image provider or the key set changes.
  useEffect(() => {
    let on = true;
    hub.call<{ providers: ImageProvider[] }>("image.catalog", {}, 15000)
      .then((r) => on && setImageCatalog(Array.isArray(r?.providers) ? r.providers : []))
      .catch(() => {});
    return () => { on = false; };
  }, [hub, defaults.image_provider, keys]);

  const activeProvider = keys.find((k) => k.active)?.label || defaults.provider || "";
  const model = defaults.model || "";
  const isOpenRouter = (defaults.base_url || "").includes("openrouter.ai");

  const providerOptions: SelectOption[] = keys.map((k) => ({ value: k.label, label: k.label, note: k.provider || undefined }));

  const modelOptions: SelectOption[] = useMemo(() => {
    const capNote = (m: ModelInfo) => `${t("cap-tools-short")}${m.tools ? "✓" : "✗"} · ${t("cap-vision-short")}${m.vision ? "✓" : "✗"}`;
    const recIds = new Set(RECOMMENDED.map((r) => r.id));
    const rec: SelectOption[] = RECOMMENDED.map((r) => ({ value: r.id, label: r.id, note: t(r.note as TKey), group: t("model-recommended") }));
    const rest = models
      .filter((m) => !recIds.has(m.id))
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((m) => ({ value: m.id, label: m.id, note: capNote(m), group: providerOf(m.id) }));
    return [...rec, ...rest];
  }, [models, t]);

  const persist = async (patch: Record<string, string>, useKey?: string) => {
    setBusy(true); setSavedTick(false); setTest(null);
    try {
      if (useKey) await hub.call("defaults.use_key", { label: useKey }, 15000);
      else await hub.call("defaults.set", patch, 15000);
      await refresh();
      setSavedTick(true);
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally { setBusy(false); }
  };

  const runTest = async () => {
    setTesting(true); setTest(null);
    try { setTest(await hub.call<TestResult>("key.test", {}, 60000)); }
    catch (e) { setTest({ ok: false, error: { kind: (e as { data?: { kind?: string } })?.data?.kind } }); }
    finally { setTesting(false); }
  };

  return (
    <div className="settings-pane on">
      <h2>{t("set-model")}</h2>
      <div className="sub">{t("model-pane-sub")}</div>
      <div className="scope-note">{t("default-scope")}</div>

      <div className="model-boxes">
        {/* Provider first. With no provider/key configured, the box is a clear
            call-to-action that jumps to the Providers pane — and NOTHING below it
            shows until a provider with a key is active. */}
        <label className="model-box">
          <span className="mb-lbl">{t("provider")}</span>
          {keys.length === 0 ? (
            <button className="okline bad small model-nokey" onClick={() => nav("#/settings/keys")}>{t("model-no-key")}</button>
          ) : (
            <Select value={activeProvider} options={providerOptions} onChange={(v) => void persist({}, v)} placeholder={t("provider")} />
          )}
        </label>

        {/* Model + the provider-specific knobs only appear once a provider is set.
            OpenRouter reports each model's real context window (no manual size) and
            wraps the unified reasoning param (reasoning shown); every other provider
            can't report a window, so it gets the manual 上下文长度 and no reasoning. */}
        {defaults.has_key && (
          <>
            <label className="model-box">
              <span className="mb-lbl">{t("model-label")}</span>
              <Select value={model} options={modelOptions} onChange={(v) => v.trim() && void persist({ model: v.trim() })} placeholder={t("model-other-ph")} search allowCustom />
              {modelsStale && <span className="model-stale-note">{t("model-list-stale")}</span>}
            </label>

            {!isOpenRouter && (
              <label className="model-box ctx-box">
                <span className="mb-lbl">{t("model-ctx-label")} <i>{t("model-ctx-note")}</i></span>
                <div className="input-like ctx-input">
                  <input
                    type="number" min="0" placeholder="0" value={ctx}
                    onChange={(e) => setCtx(e.target.value)}
                    onBlur={(e) => { const v = e.target.value.trim(); if (v !== String(defaults.model_context || "")) void persist({ model_context: v || "0" }); }}
                  />
                </div>
              </label>
            )}

            <label className="model-box">
              <span className="mb-lbl">{t("model-refresh-label")} <i>{t("model-refresh-note")}</i></span>
              <div className="input-like ctx-input">
                <input
                  type="number" min="0" step="0.5" placeholder="1" value={refreshDays}
                  onChange={(e) => setRefreshDays(e.target.value)}
                  onBlur={(e) => {
                    const days = parseFloat(e.target.value.trim());
                    const secs = Number.isFinite(days) && days > 0 ? String(Math.round(days * 86400)) : "0";
                    if (secs !== String(defaults.model_refresh_interval || "0")) void persist({ model_refresh_interval: secs });
                  }}
                />
              </div>
            </label>

            <div className="model-test">
              <button className="btn soft sm" disabled={testing} onClick={() => void runTest()}>
                {testing ? t("testing") : t("test")}
              </button>
              {test && (
                <span className={"okline" + (test.ok ? "" : " bad")}>
                  {test.ok ? `${t("connected")} · ${t("cap-tools-short")}${test.capabilities?.tools ? "✓" : "✗"} · ${t("cap-vision-short")}${test.capabilities?.vision ? "✓" : "✗"}` : "✗ " + errText(t, test.error)}
                </span>
              )}
            </div>

            {isOpenRouter && (
              <div className="model-reasoning">
                <span className="mb-lbl" id="reasoning-lbl">{t("reasoning-label")}</span>
                <Segmented
                  ariaLabelledby="reasoning-lbl"
                  value={defaults.reasoning || "medium"}
                  options={REASONING.map((r) => ({ value: r, label: t(("reason-" + r) as TKey) }))}
                  onChange={(r) => void persist({ reasoning: r })}
                />
                <span className="reason-hint">{t("reasoning-or-only")}</span>
              </div>
            )}
          </>
        )}
      </div>

      {/* per-function model overrides — Hermes' auxiliary-models pattern. Hidden
          until a main provider exists (the overrides default to the main model). */}
      {defaults.has_key && (
        <TaskModels
          values={defaults as unknown as Record<string, string | undefined>}
          imageCatalog={imageCatalog}
          keys={keys}
          modelOptions={modelOptions}
          mainProvider={activeProvider}
          onApplyImage={(provider, model) => void persist({ image_provider: provider, image_model: model })}
          onApplyTaskProvider={(pf, mf, provider, model) => void persist({ [pf]: provider, [mf]: model })}
        />
      )}

      <div className="model-save-state">
        {busy ? <span className="lm-thinking">{t("saving")}</span> : savedTick ? <span className="okline">✓ {t("saved")}</span> : null}
      </div>

      {/* background-removal models — lives at the very bottom, set apart */}
      <MatteSection />
    </div>
  );
}
