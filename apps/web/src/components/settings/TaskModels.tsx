/* TaskModels — a config-driven list of per-function model overrides, after
 * Hermes' "Auxiliary models" section. Each task defaults to the main model and
 * can be overridden with its own model (catalog-picked or free-typed). One
 * component, one row component, one task table — add a function by adding a row
 * to TASKS, not by writing more UI.
 *
 * Each task maps to a defaults field; applying writes defaults.set({[field]: v})
 * (empty = "use main model"). The catalog (the main provider's models.list) is
 * passed in so every row shares one fetch.
 *
 * The "imagegen" task is special: image generation runs on its OWN provider
 * (Volcano Ark / Alibaba DashScope / OpenAI / OpenRouter), not the main text
 * provider — so it renders a PROVIDER + MODEL picker fed by image.catalog and
 * persists BOTH image_provider and image_model together. */

import { useState } from "react";
import { useT, type TKey } from "../../i18n";
import { Select, type SelectOption } from "./Select";

export interface ImageProvider {
  id: string;
  label: string;
  models: { id: string; label: string }[];
  has_key: boolean;
  active: boolean;
}

export interface KeyRow { label: string; provider: string; base_url: string; has_key: boolean; active: boolean }

export interface TaskModel {
  key: string;
  labelKey: TKey;
  descKey: TKey;
  field: string; // the defaults.* MODEL field it persists to
  // image = image.catalog (火山/百炼 image providers); keyprovider = its OWN text
  // provider, picked from the keyring (read-image / card / image-prompt — and any
  // future modality, e.g. audio — all run on their own provider+model).
  source: "image" | "keyprovider";
  providerField?: string; // the defaults.* PROVIDER field (keyprovider tasks only)
}

/* The functions that can run on their own model. Order = display order.
   Every "keyprovider" task follows ONE pattern: pick a saved provider + a model,
   persisting {<task>_provider, <task>_model}. Adding audio later = one more row. */
export const TASKS: ReadonlyArray<TaskModel> = [
  { key: "vision", labelKey: "aux-vision", descKey: "aux-vision-desc", field: "vision_model", providerField: "vision_provider", source: "keyprovider" },
  { key: "card", labelKey: "aux-card", descKey: "aux-card-desc", field: "card_model", providerField: "card_provider", source: "keyprovider" },
  { key: "imageprompt", labelKey: "aux-imgprompt", descKey: "aux-imgprompt-desc", field: "image_prompt_model", providerField: "image_prompt_provider", source: "keyprovider" },
  { key: "imagegen", labelKey: "aux-imagegen", descKey: "aux-imagegen-desc", field: "image_model", source: "image" },
];

export function TaskModels({
  values,
  imageCatalog,
  keys,
  modelOptions,
  onApplyImage,
  onApplyTaskProvider,
}: {
  values: Record<string, string | undefined>;
  imageCatalog: ImageProvider[];
  keys: KeyRow[];
  // The main text-model catalogue (recommended + the active provider's models) —
  // shown as the per-task model list so the user picks from a dropdown instead of
  // having to type the id. Free-typing still works (allowCustom) for a model the
  // catalogue doesn't list or a different provider.
  modelOptions: SelectOption[];
  onApplyImage: (provider: string, model: string) => void;
  onApplyTaskProvider: (providerField: string, modelField: string, provider: string, model: string) => void;
}) {
  const t = useT();
  return (
    <div className="aux-sec">
      <h3 className="aux-title">{t("aux-title")}</h3>
      <div className="aux-subline">{t("aux-sub")}</div>
      {TASKS.map((task) =>
        task.source === "image" ? (
          <ImageModelRow
            key={task.key}
            task={task}
            provider={values.image_provider || ""}
            model={values.image_model || ""}
            providers={imageCatalog}
            onApply={onApplyImage}
          />
        ) : (
          <KeyProviderRow
            key={task.key}
            task={task}
            provider={values[task.providerField || ""] || ""}
            model={values[task.field] || ""}
            keys={keys}
            modelOptions={modelOptions}
            onApply={(p, m) => onApplyTaskProvider(task.providerField || "", task.field, p, m)}
          />
        ),
      )}
    </div>
  );
}

/* Read-image row: pick a saved provider key (the chara's text provider is NOT
   reused — vision needs no prompt cache), then type the vision model id. Applying
   persists vision_provider + vision_model; "use main" clears both (no aux vision).
   So a chara on OpenRouter can read images via, e.g., an Alibaba vision model. */
function KeyProviderRow({
  task,
  provider,
  model,
  keys,
  modelOptions,
  onApply,
}: {
  task: TaskModel;
  provider: string;
  model: string;
  keys: KeyRow[];
  modelOptions: SelectOption[];
  onApply: (provider: string, model: string) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [pid, setPid] = useState(provider);
  const [draft, setDraft] = useState(model);

  const curKey = keys.find((k) => k.label === provider);
  const editKey = keys.find((k) => k.label === pid);
  const provOptions: SelectOption[] = keys.map((k) => ({
    value: k.label,
    label: k.label,
    note: k.has_key ? "✓ " + t("img-key-ready") : t("img-key-missing"),
  }));
  const begin = () => { setPid(provider); setDraft(model); setEditing(true); };

  return (
    <div className="aux-row">
      <div className="aux-main">
        <div className="aux-head">
          <b>{t(task.labelKey)}</b>
          <span className="aux-desc">{t(task.descKey)}</span>
        </div>
        <div className="aux-cur">
          {model ? (
            <>
              {curKey && <span className="img-prov-tag">{curKey.label}</span>}
              <code>{model}</code>
              {curKey && !curKey.has_key && <span className="img-nokey-warn">· {t("img-key-missing")}</span>}
            </>
          ) : (
            t("aux-auto")
          )}
        </div>
      </div>
      {!editing ? (
        <div className="aux-acts">
          {model && <button className="btn text sm" onClick={() => onApply("", "")}>{t("aux-use-main")}</button>}
          <button className="btn text sm" onClick={begin}>{t("aux-change")}</button>
        </div>
      ) : (
        <div className="aux-edit img-edit">
          <Select value={pid} options={provOptions} onChange={setPid} placeholder={t("provider")} />
          <Select value={draft} options={modelOptions} onChange={setDraft} search allowCustom placeholder={t("model-other-ph")} />
          {editKey && !editKey.has_key && <div className="img-prov-hint">{t("img-prov-hint")}</div>}
          <div className="acts">
            <button
              className="btn primary sm"
              disabled={!pid || !draft.trim()}
              onClick={() => { onApply(pid, draft.trim()); setEditing(false); }}
            >{t("aux-apply")}</button>
            <button className="btn text sm" onClick={() => setEditing(false)}>{t("cancel")}</button>
          </div>
        </div>
      )}
    </div>
  );
}

/* Image-gen row: pick a provider, then one of that provider's models (free-typing
   allowed). Shows whether the chosen provider has a key (set in the Providers
   pane). Applying persists image_provider + image_model together. */
function ImageModelRow({
  task,
  provider,
  model,
  providers,
  onApply,
}: {
  task: TaskModel;
  provider: string;
  model: string;
  providers: ImageProvider[];
  onApply: (provider: string, model: string) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  // The provider actually in effect (explicit, else the catalogue's active one).
  const activeId = provider || providers.find((p) => p.active)?.id || (providers[0]?.id ?? "");
  const [pid, setPid] = useState(activeId);
  const [draft, setDraft] = useState(model);

  const curProv = providers.find((p) => p.id === (provider || activeId));
  const editProv = providers.find((p) => p.id === pid);

  const provOptions: SelectOption[] = providers.map((p) => ({
    value: p.id,
    label: p.label,
    note: p.has_key ? "✓ " + t("img-key-ready") : t("img-key-missing"),
  }));
  const modelOptions: SelectOption[] = (editProv?.models || []).map((m) => ({ value: m.id, label: m.label, note: m.id }));

  const begin = () => { setPid(activeId); setDraft(model); setEditing(true); };

  return (
    <div className="aux-row">
      <div className="aux-main">
        <div className="aux-head">
          <b>{t(task.labelKey)}</b>
          <span className="aux-desc">{t(task.descKey)}</span>
        </div>
        <div className="aux-cur">
          {model ? (
            <>
              {curProv && <span className="img-prov-tag">{curProv.label}</span>}
              <code>{model}</code>
              {curProv && !curProv.has_key && <span className="img-nokey-warn">· {t("img-key-missing")}</span>}
            </>
          ) : (
            t("img-unset")
          )}
        </div>
      </div>
      {!editing ? (
        <div className="aux-acts">
          <button className="btn text sm" onClick={begin}>{t("aux-change")}</button>
        </div>
      ) : (
        <div className="aux-edit img-edit">
          <Select
            value={pid}
            options={provOptions}
            onChange={(v) => { setPid(v); setDraft(""); }}
            placeholder={t("provider")}
          />
          <Select
            value={draft}
            options={modelOptions}
            onChange={setDraft}
            search
            allowCustom
            placeholder={t("image-gen-ph")}
          />
          {editProv && !editProv.has_key && <div className="img-prov-hint">{t("img-prov-hint")}</div>}
          <div className="acts">
            <button
              className="btn primary sm"
              disabled={!pid || !draft.trim()}
              onClick={() => { onApply(pid, draft.trim()); setEditing(false); }}
            >{t("aux-apply")}</button>
            <button className="btn text sm" onClick={() => setEditing(false)}>{t("cancel")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
