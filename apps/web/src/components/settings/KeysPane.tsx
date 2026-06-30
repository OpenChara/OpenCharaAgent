/* Providers pane (formerly Keys) — a Hermes-style provider list: one row per
 * provider, one key per provider. We offer OpenRouter (preset base_url) +
 * self-registered Local/Custom OpenAI-compatible endpoints (each needs a name +
 * base_url + key — this is how you point at a relay / a self-hosted model). A
 * filled dot marks the active text provider (chosen here or in the Model pane);
 * a hollow dot means a saved-but-inactive key. Keys never echo back — a saved
 * row shows a masked chip.
 *
 * Backed by keys.list/save/delete + defaults.use_key. */

import { useCallback, useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { errText, rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";
import { PROVIDER_PRESETS as PRESETS } from "../../lib/providers";

interface KeyRowData { label: string; provider: string; base_url: string; model: string; has_key: boolean; active: boolean }
interface TestResult { ok?: boolean; error?: { kind?: string; detail?: string } }

export function KeysPane() {
  const t = useT();
  const { hub } = useHubApi();
  const [rows, setRows] = useState<KeyRowData[] | null>(null);
  const [busy, setBusy] = useState<string>("");
  const [addingCustom, setAddingCustom] = useState(false);
  const [cust, setCust] = useState({ name: "", base_url: "", key: "" });

  const refresh = useCallback(async () => {
    try { setRows(await hub.call<KeyRowData[]>("keys.list", {}, 15000)); }
    catch (e) { deckToast(rpcErrText(t, e as { message?: string }), true); }
  }, [hub, t]);

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

  const activateKey = async (label: string) => {
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
            onUse={() => activateKey(pp.label)} onTest={() => testKey(pp.label)}
          />
        ))}
        {customRows.map((r) => (
          <ProviderRow
            key={r.label} name={r.label} desc={r.base_url} custom row={r} busy={busy === r.label}
            onSave={(k) => saveKey(r.label, r.provider, r.base_url, k, false)}
            onUse={() => activateKey(r.label)} onDelete={() => del(r.label)} onTest={() => testKey(r.label)}
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
