/* Providers pane (formerly Keys) — a Hermes-style provider list: one row per
 * provider, one key per provider. We offer OpenRouter (preset base_url) +
 * self-registered Local/Custom OpenAI-compatible endpoints (each needs a name +
 * base_url + key — this is how you point at a relay / a self-hosted model). A
 * filled dot marks the active text provider (chosen here or in the Model pane);
 * a hollow dot means a saved-but-inactive key. The image-gen key lives in its own
 * row at the bottom. Keys never echo back — a saved row shows a masked chip.
 *
 * Backed by keys.list/save/delete + defaults.use_key (text) and defaults.get/set
 * image_api_key (image). One visual language across both. */

import { useCallback, useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { errText, rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";

interface KeyRowData { label: string; provider: string; base_url: string; model: string; has_key: boolean; active: boolean }
interface TestResult { ok?: boolean; error?: { kind?: string; detail?: string } }

/* Curated OpenAI-compatible providers offered as preset rows (one key each).
   base_url is editable later via a custom endpoint if a region/path differs. */
const PRESETS: ReadonlyArray<{ label: string; provider: string; base_url: string; descKey: string }> = [
  { label: "OpenRouter", provider: "openrouter", base_url: "https://openrouter.ai/api/v1", descKey: "prov-openrouter-desc" },
  { label: "OpenAI", provider: "openai", base_url: "https://api.openai.com/v1", descKey: "prov-openai-desc" },
  { label: "火山引擎", provider: "volcano", base_url: "https://ark.cn-beijing.volces.com/api/v3", descKey: "prov-volcano-desc" },
  { label: "混元", provider: "hunyuan", base_url: "https://api.hunyuan.cloud.tencent.com/v1", descKey: "prov-hunyuan-desc" },
  { label: "阿里云", provider: "dashscope", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", descKey: "prov-aliyun-desc" },
];

interface ImageProviderRow { id: string; label: string; has_key: boolean; active: boolean }

export function KeysPane() {
  const t = useT();
  const { hub } = useHubApi();
  const [rows, setRows] = useState<KeyRowData[] | null>(null);
  const [busy, setBusy] = useState<string>("");
  const [addingCustom, setAddingCustom] = useState(false);
  const [cust, setCust] = useState({ name: "", base_url: "", key: "" });
  const [imgProviders, setImgProviders] = useState<ImageProviderRow[]>([]);

  const refreshImage = useCallback(async () => {
    try {
      const r = await hub.call<{ providers: ImageProviderRow[] }>("image.catalog", {}, 15000);
      setImgProviders(Array.isArray(r?.providers) ? r.providers : []);
    } catch { /* status list is best-effort */ }
  }, [hub]);

  const refresh = useCallback(async () => {
    try { setRows(await hub.call<KeyRowData[]>("keys.list", {}, 15000)); }
    catch (e) { deckToast(rpcErrText(t, e as { message?: string }), true); }
    void refreshImage();
  }, [hub, t, refreshImage]);

  useEffect(() => { void refresh(); }, [refresh]);

  const saveKey = async (label: string, provider: string, base_url: string, api_key: string, makeActive: boolean) => {
    setBusy(label);
    try {
      const next = await hub.call<KeyRowData[]>("keys.save", { label, provider, base_url, api_key }, 20000);
      // auto-activate when nothing is active yet (so a first key "just works")
      if (makeActive || !next.some((r) => r.active)) await hub.call("defaults.use_key", { label }, 15000);
      await refresh();
    } catch (e) { deckToast(rpcErrText(t, e as { message?: string }), true); }
    finally { setBusy(""); }
  };

  const useKey = async (label: string) => {
    setBusy(label);
    setRows((p) => p?.map((r) => ({ ...r, active: r.label === label })) ?? p); // optimistic
    try { await hub.call("defaults.use_key", { label }, 15000); await refresh(); }
    catch (e) { deckToast(rpcErrText(t, e as { message?: string }), true); await refresh(); }
    finally { setBusy(""); }
  };

  const del = async (label: string) => {
    setBusy(label);
    try { setRows(await hub.call<KeyRowData[]>("keys.delete", { label }, 15000)); }
    catch (e) { deckToast(rpcErrText(t, e as { message?: string }), true); }
    finally { setBusy(""); }
  };

  const submitCustom = async () => {
    const name = cust.name.trim(), base = cust.base_url.trim(), key = cust.key.trim();
    if (!name || !base || !key) { deckToast(t("keys-need-key"), true); return; }
    await saveKey(name, "openai_compatible", base, key, true);
    setCust({ name: "", base_url: "", key: "" });
    setAddingCustom(false);
  };

  // Per-row connectivity test: resolves the saved key's secret server-side and
  // tests it (key.test with a label). Errors come back as a classified shape.
  const testKey = async (label: string): Promise<TestResult> => {
    try { return await hub.call<TestResult>("key.test", { label }, 60000); }
    catch (e) { return { ok: false, error: { kind: (e as { data?: { kind?: string } })?.data?.kind } }; }
  };

  const byLabel = (l: string) => rows?.find((r) => r.label === l) || null;
  const customRows = (rows || []).filter((r) => !PRESETS.some((p) => p.label === r.label));

  return (
    <div className="settings-pane on prov-pane">
      <h2>{t("prov-title")}</h2>
      <div className="sub">{t("prov-sub")}</div>

      <div className="prov-list">
        {PRESETS.map((pp) => (
          <ProviderRow
            key={pp.label} name={pp.label} desc={t(pp.descKey as Parameters<typeof t>[0])} row={byLabel(pp.label)}
            busy={busy === pp.label}
            onSave={(k) => saveKey(pp.label, pp.provider, pp.base_url, k, false)}
            onUse={() => useKey(pp.label)} onTest={() => testKey(pp.label)}
          />
        ))}
        {customRows.map((r) => (
          <ProviderRow
            key={r.label} name={r.label} desc={r.base_url} custom row={r} busy={busy === r.label}
            onSave={(k) => saveKey(r.label, r.provider, r.base_url, k, false)}
            onUse={() => useKey(r.label)} onDelete={() => del(r.label)} onTest={() => testKey(r.label)}
          />
        ))}
      </div>

      {addingCustom ? (
        <div className="prov-add-form">
          <Field label={t("prov-custom-name")} ph={t("prov-custom-name-ph")} value={cust.name} onChange={(v) => setCust({ ...cust, name: v })} />
          <Field label={t("prov-custom-base")} ph={t("prov-custom-base-ph")} mono value={cust.base_url} onChange={(v) => setCust({ ...cust, base_url: v })} />
          <Field label={t("key-label")} type="password" mono value={cust.key} onChange={(v) => setCust({ ...cust, key: v })} />
          <div className="acts">
            <button className="btn primary sm" onClick={() => void submitCustom()}>{t("prov-save")}</button>
            <button className="btn text sm" onClick={() => { setAddingCustom(false); setCust({ name: "", base_url: "", key: "" }); }}>{t("cancel")}</button>
          </div>
        </div>
      ) : (
        <button className="btn soft prov-add-btn" onClick={() => setAddingCustom(true)}>{t("prov-add-custom")}</button>
      )}

      <h2 className="prov-section">{t("prov-image-section")}</h2>
      <div className="sub">{t("prov-image-desc")}</div>
      <div className="prov-list">
        {imgProviders.map((p) => (
          <div key={p.id} className={"prov-row" + (p.active ? " active" : "")}>
            <span className={"prov-dot" + (p.active ? " on" : p.has_key ? " set" : "")} />
            <div className="prov-meta">
              <div className="prov-name">{p.label}{p.active && <span className="prov-badge">{t("img-active")}</span>}</div>
              <div className="prov-desc">{p.has_key ? t("img-key-ready") : t("img-key-missing-row")}</div>
            </div>
            <div className="prov-key">
              <span className={"okline" + (p.has_key ? "" : " bad")}>{p.has_key ? "✓" : "✗"}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ProviderRow({
  name, desc, row, busy, custom, onSave, onUse, onDelete, onTest,
}: {
  name: string; desc?: string; row: KeyRowData | null; busy: boolean; custom?: boolean;
  onSave: (key: string) => void; onUse: () => void; onDelete?: () => void; onTest?: () => Promise<TestResult>;
}) {
  const t = useT();
  const has = !!row?.has_key;
  const active = !!row?.active;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);

  const save = () => { if (draft.trim()) { onSave(draft.trim()); setDraft(""); setEditing(false); } };
  const test = async () => {
    if (!onTest) return;
    setTesting(true); setResult(null);
    try { setResult(await onTest()); } finally { setTesting(false); }
  };

  return (
    <div className={"prov-row" + (active ? " active" : "")}>
      <span className={"prov-dot" + (active ? " on" : has ? " set" : "")} />
      <div className="prov-meta">
        <div className="prov-name">{name}{active && <span className="prov-badge">{t("prov-active")}</span>}</div>
        {desc && <div className="prov-desc">{desc}</div>}
        {(testing || result) && (
          <div className="prov-desc">
            {testing ? <><span className="spin" /> {t("test")}…</>
              : <span className={"okline" + (result?.ok ? "" : " bad")}>
                  {result?.ok ? t("connected") : "✗ " + errText(t, result?.error)}
                </span>}
          </div>
        )}
      </div>
      <div className="prov-key">
        {has && !editing ? (
          <>
            <span className="prov-masked">••••••••</span>
            {!active && <button className="btn text sm" disabled={busy} onClick={onUse}>{t("prov-use")}</button>}
            {onTest && <button className="btn text sm" disabled={busy || testing} onClick={() => void test()}>{t("test")}</button>}
            <button className="btn text sm" onClick={() => { setDraft(""); setEditing(true); }}>{t("prov-change-key")}</button>
            {custom && onDelete && <button className="btn text sm" disabled={busy} onClick={onDelete}>✕</button>}
          </>
        ) : (
          <>
            <input
              className="prov-input mono" type="password" placeholder={t("key-ph")} value={draft} autoFocus={editing}
              onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()}
            />
            <button className="btn primary sm" disabled={busy || !draft.trim()} onClick={save}>{busy ? <span className="spin" /> : t("prov-save")}</button>
            {editing && <button className="btn text sm" onClick={() => setEditing(false)}>{t("cancel")}</button>}
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, ph, type = "text", mono }: { label: string; value: string; onChange: (v: string) => void; ph?: string; type?: "text" | "password"; mono?: boolean }) {
  return (
    <label className="key-field">
      <span>{label}</span>
      <input type={type} className={mono ? "mono" : undefined} placeholder={ph} value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}
