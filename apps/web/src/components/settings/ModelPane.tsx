/* ModelPane — the model setup pane, a React port of app.js:1451 setupPane (the
 * settings·模型 use; the first-run variant is a separate overlay per §6).
 * Provider presets + base-url + API key (key.test) + default model, saved via
 * defaults.set. After save, if the backend reports charas whose key could be
 * updated, the apply-key prompt offers to propagate it (defaults.apply_key).
 *
 * Binding UI rule: Test shows a working state + the result inline; Save (继续)
 * shows a working state and surfaces errors. */

import { useEffect, useMemo, useState } from "react";
import { useT, useLang } from "../../i18n";
import { useHub, useHubApi } from "../../state/hub";
import { errText, rpcErrText } from "../../lib/status";
import { Caps } from "../deck/Caps";
import { DeckModal } from "../ui/DeckModal";
import { deckToast } from "../ui/deckToast";
import type { ModelInfo } from "../deck/types";

interface Preset {
  provider?: string;
  base_url?: string;
  model?: string;
}
interface Defaults {
  provider?: string;
  base_url?: string;
  model?: string;
  has_key?: boolean;
}
interface KeyTestResult {
  ok?: boolean;
  error?: { kind?: string };
  capabilities?: { tools?: boolean; writing?: boolean; vision?: boolean };
}
interface KeyCandidate {
  name: string;
  char_name?: string;
  model?: string;
}

const BUILTIN_PROVIDERS: ReadonlyArray<readonly [string, string, string, boolean]> = [
  ["OpenRouter", "OpenRouter", "or-desc", true],
  ["OpenAI", "OpenAI", "prov-openai-desc", false],
  ["Ollama (local)", "Ollama", "prov-ollama-desc", false],
  ["_custom", "OpenAI-compatible", "prov-compat-desc", false],
] as const;

export function ModelPane() {
  const t = useT();
  const { lang } = useLang();
  const { hub, snapshot, refresh } = useHub();
  const defaults = (snapshot?.defaults as Defaults) || {};
  const presets = (snapshot?.presets as Record<string, Preset>) || {};
  const [models, setModels] = useState<ModelInfo[]>([]);

  // models aren't on hub.state — fetch the catalog once (app.js modelsCached).
  useEffect(() => {
    let alive = true;
    hub
      .call<ModelInfo[]>("models.list", {}, 30000)
      .then((m) => alive && setModels(Array.isArray(m) ? m : []))
      .catch(() => alive && setModels([]));
    return () => {
      alive = false;
    };
  }, [hub]);

  const [provider, setProvider] = useState(defaults.provider || "openrouter");
  const [pickedPreset, setPickedPreset] = useState("OpenRouter");
  const [baseUrl, setBaseUrl] = useState(defaults.base_url || "https://openrouter.ai/api/v1");
  const [model, setModel] = useState(defaults.model || "deepseek/deepseek-v4-flash");
  const [apiKey, setApiKey] = useState("");
  const [showMore, setShowMore] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<KeyTestResult | null>(null);
  const [saving, setSaving] = useState(false);
  const [candidates, setCandidates] = useState<KeyCandidate[] | null>(null);

  const pickProvider = (key: string) => {
    const p = presets[key] || {};
    setProvider(p.provider || "openai_compatible");
    setBaseUrl(p.base_url || "");
    if (p.model) setModel(p.model);
    setPickedPreset(key);
    setTest(null);
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    const params = {
      provider,
      base_url: baseUrl,
      api_key: apiKey.trim(),
      model: model.trim(),
    };
    try {
      const r = await hub.call<KeyTestResult>("key.test", params, 60000);
      setTest(r);
    } catch (e) {
      setTest({ ok: false, error: { kind: (e as { data?: { kind?: string } })?.data?.kind } });
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    const payload: Record<string, unknown> = {
      provider,
      base_url: baseUrl,
      model: model.trim() || model,
      ui_lang: lang,
    };
    if (apiKey.trim()) payload.api_key = apiKey.trim();
    try {
      const saved = await hub.call<{ key_update_candidates?: KeyCandidate[] }>("defaults.set", payload, 15000);
      await refresh();
      deckToast(t("saved"));
      if (saved && saved.key_update_candidates && saved.key_update_candidates.length) {
        setCandidates(saved.key_update_candidates);
      }
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setSaving(false);
    }
  };

  const okBad = test && test.ok === false;

  return (
    <div className="settings-pane on" id="pane-model">
      <h1>{t("setup-title")}</h1>
      <div className="sub">{t("setup-sub")}</div>
      <div className="scope-note">{t("default-scope")}</div>
      <div className="no-fallback-row">⊘ {t("no-fallback")}</div>

      {BUILTIN_PROVIDERS.slice(0, 2).map(([key, label, desc, rec]) => (
        <ProviderRow key={key} on={pickedPreset === key} label={label} desc={t(desc)} rec={rec} onClick={() => pickProvider(key)} />
      ))}
      <button className="provider more" onClick={() => setShowMore((v) => !v)}>
        {t("more-providers")}
      </button>
      {showMore && (
        <div>
          {BUILTIN_PROVIDERS.slice(2).map(([key, label, desc, rec]) => (
            <ProviderRow key={key} on={pickedPreset === key} label={label} desc={t(desc)} rec={rec} onClick={() => pickProvider(key)} />
          ))}
        </div>
      )}

      {pickedPreset === "_custom" && (
        <div className="keyrow">
          <div className="input-like">
            <input placeholder={t("base-ph")} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </div>
        </div>
      )}

      <div className="keyrow">
        <div className="input-like">
          <input
            type="password"
            placeholder={defaults.has_key ? "••••••••  (saved)" : t("key-ph")}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>
        <button className="btn soft" disabled={testing} onClick={() => void runTest()}>
          {testing ? t("testing") : t("test")}
        </button>
      </div>

      {(testing || test) && (
        <div className="test-result show">
          <div className={"okline" + (okBad ? " bad" : "")}>
            {testing ? t("testing") : test && test.ok ? t("connected") : "✗ " + errText(t, test?.error)}
          </div>
          <div className="modelrow">
            <span>{t("default-model")}</span>
            <div className="input-like" style={{ flex: 1 }}>
              <input list="model-list" value={model} onChange={(e) => setModel(e.target.value)} />
            </div>
          </div>
          {test && test.ok && <Caps caps={test.capabilities || null} />}
          <div className="cap-hint">{t("cap-hint")}</div>
        </div>
      )}
      <datalist id="model-list">
        {models.slice(0, 400).map((m) => (
          <option key={m.id} value={m.id} />
        ))}
      </datalist>

      <div className="setup-acts">
        <span />
        <div className="grow" />
        <button className="btn primary big" disabled={saving} onClick={() => void save()}>
          {saving ? <span className="spin" /> : t("continue")}
        </button>
      </div>

      {candidates && <KeyUpdatePrompt candidates={candidates} onClose={() => setCandidates(null)} />}
    </div>
  );
}

function ProviderRow({
  on,
  label,
  desc,
  rec,
  onClick,
}: {
  on: boolean;
  label: string;
  desc?: string;
  rec: boolean;
  onClick: () => void;
}) {
  const t = useT();
  return (
    <button className={"provider" + (on ? " on" : "")} onClick={onClick}>
      <div>
        <div className="pname">
          {label}
          {rec && <span className="rec">{t("rec")}</span>}
        </div>
        {desc && <div className="pdesc">{desc}</div>}
      </div>
      <div className="radio" />
    </button>
  );
}

/* The "propagate the new key to these charas" prompt (app.js:1589 promptKeyUpdate),
   rendered as a modal. Optimistic per-row checkboxes; Apply shows a working state. */
function KeyUpdatePrompt({ candidates, onClose }: { candidates: KeyCandidate[]; onClose: () => void }) {
  const t = useT();
  const { hub, refresh } = useHubApi();
  const [checked, setChecked] = useState<Set<string>>(() => new Set(candidates.map((c) => c.name)));
  const [applying, setApplying] = useState(false);

  const allOn = useMemo(() => candidates.every((c) => checked.has(c.name)), [candidates, checked]);

  const toggle = (name: string) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const apply = async () => {
    const names = candidates.filter((c) => checked.has(c.name)).map((c) => c.name);
    if (!names.length) return;
    setApplying(true);
    try {
      const r = await hub.call<{ updated?: string[] }>("defaults.apply_key", { names }, 30000);
      deckToast(t("key-update-done", { n: (r.updated || []).length }));
      await refresh();
      onClose();
    } catch (e) {
      setApplying(false);
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };

  return (
    <DeckModal open variant="sheet" onClose={onClose}>
      <h2>{t("key-update-title")}</h2>
      <div className="sub">{t("key-update-sub")}</div>
      <label className="check-row select-all">
        <input
          type="checkbox"
          checked={allOn}
          onChange={() => setChecked(allOn ? new Set() : new Set(candidates.map((c) => c.name)))}
        />
        <b>{t("select-all")}</b>
      </label>
      <div className="key-update-list">
        {candidates.map((c) => (
          <label className="check-row" key={c.name}>
            <input type="checkbox" checked={checked.has(c.name)} onChange={() => toggle(c.name)} />
            <span>
              <b>{c.char_name || c.name}</b> <small>{c.name}</small>
            </span>
            {c.model && <code>{c.model}</code>}
          </label>
        ))}
      </div>
      <div className="acts" style={{ marginTop: 16 }}>
        <button className="btn text" onClick={onClose}>{t("later")}</button>
        <div className="grow" />
        <button className="btn primary big" disabled={applying || checked.size === 0} onClick={() => void apply()}>
          {applying ? <span className="spin" /> : t("key-update-apply", { n: checked.size })}
        </button>
      </div>
    </DeckModal>
  );
}
