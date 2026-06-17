import { useCallback, useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";

/* R10 — the saved-keys store, ported from the deleted front/web/app.js. Keep
   several named LLM keys and switch the active default with one click. The
   secret value NEVER travels back from the server (rows carry only has_key);
   adding a key sends it once via keys.save. Wired into the model Settings pane. */

interface KeyRow {
  label: string;
  provider: string;
  base_url: string;
  model: string;
  has_key: boolean;
  active: boolean;
}

export function KeysPane() {
  const t = useT();
  const { hub } = useHubApi();
  const [rows, setRows] = useState<KeyRow[] | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ label: "", provider: "", base_url: "", api_key: "", model: "" });
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRows(await hub.call<KeyRow[]>("keys.list", {}, 15000));
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  }, [hub, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setBusyLabel = (label: string, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(label);
      else next.delete(label);
      return next;
    });

  const makeDefault = async (label: string) => {
    if (busy.has(label)) return;
    setBusyLabel(label, true);
    // optimistic: flip active locally
    setRows((prev) => prev?.map((r) => ({ ...r, active: r.label === label })) ?? prev);
    try {
      await hub.call("defaults.use_key", { label }, 15000);
      await refresh();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
      await refresh(); // revert to server truth
    } finally {
      setBusyLabel(label, false);
    }
  };

  const remove = async (label: string) => {
    if (busy.has(label)) return;
    setBusyLabel(label, true);
    try {
      setRows(await hub.call<KeyRow[]>("keys.delete", { label }, 15000));
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setBusyLabel(label, false);
    }
  };

  const submit = async () => {
    const label = form.label.trim();
    if (!label) {
      deckToast(t("keys-need-label"), true);
      return;
    }
    if (!form.api_key.trim()) {
      deckToast(t("keys-need-key"), true);
      return;
    }
    setSaving(true);
    try {
      setRows(
        await hub.call<KeyRow[]>(
          "keys.save",
          {
            label,
            provider: form.provider.trim(),
            base_url: form.base_url.trim(),
            api_key: form.api_key,
            model: form.model.trim(),
          },
          20000,
        ),
      );
      setForm({ label: "", provider: "", base_url: "", api_key: "", model: "" });
      setAdding(false);
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="keys-block">
      <div className="set-row">
        <div className="lbl">
          <span>{t("keys-title")}</span>
          <small>{t("keys-sub")}</small>
        </div>
      </div>

      {rows === null ? (
        <div className="muted small">{t("keys-loading")}</div>
      ) : rows.length === 0 ? (
        <div className="muted small">{t("keys-empty")}</div>
      ) : (
        <div className="keys-list">
          {rows.map((r) => (
            <div className={"key-row" + (r.active ? " on" : "")} key={r.label}>
              <div className="key-meta">
                <b>{r.label}</b>
                <span className="chip">{r.provider || "—"}</span>
                {r.model && <span className="muted small">{r.model}</span>}
                {!r.has_key && <span className="okline bad small">{t("keys-nokey")}</span>}
              </div>
              <div className="key-acts">
                {r.active ? (
                  <span className="chip">{t("keys-active")}</span>
                ) : (
                  <button className="btn sm" disabled={busy.has(r.label)} onClick={() => void makeDefault(r.label)}>
                    {t("keys-use")}
                  </button>
                )}
                <button className="btn soft sm" disabled={busy.has(r.label)} onClick={() => void remove(r.label)}>
                  {busy.has(r.label) ? <span className="spin" /> : "✕"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {adding ? (
        <div className="key-add">
          <input
            className="searchfield"
            placeholder={t("keys-label-ph")}
            value={form.label}
            onChange={(e) => setForm({ ...form, label: e.target.value })}
          />
          <input
            className="searchfield"
            placeholder={t("provider")}
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
          />
          <input
            className="searchfield"
            placeholder="base_url"
            value={form.base_url}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
          />
          <input
            className="searchfield"
            type="password"
            placeholder={t("key-label")}
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
          />
          <input
            className="searchfield"
            placeholder="model"
            value={form.model}
            onChange={(e) => setForm({ ...form, model: e.target.value })}
          />
          <div className="acts">
            <button className="btn primary" disabled={saving} onClick={() => void submit()}>
              {saving ? <span className="spin" /> : t("keys-add")}
            </button>
            <button className="btn text" onClick={() => setAdding(false)}>
              {t("cancel")}
            </button>
          </div>
        </div>
      ) : (
        <button className="btn soft" onClick={() => setAdding(true)}>
          ＋ {t("keys-add-new")}
        </button>
      )}
    </div>
  );
}
