/* TaskModels — a config-driven list of per-function model overrides, after
 * Hermes' "Auxiliary models" section. Each task defaults to the main model and
 * can be overridden with its own model (catalog-picked or free-typed). One
 * component, one row component, one task table — add a function by adding a row
 * to TASKS, not by writing more UI.
 *
 * Each task maps to a defaults field; applying writes defaults.set({[field]: v})
 * (empty = "use main model"). The catalog (the main provider's models.list) is
 * passed in so every row shares one fetch. */

import { useState } from "react";
import { useT, type TKey } from "../../i18n";
import { Select, type SelectOption } from "./Select";

export interface TaskModel {
  key: string;
  labelKey: TKey;
  descKey: TKey;
  field: string; // the defaults.* field it persists to
  source: "catalog" | "free"; // catalog = main provider's models.list; free = type any id
  phKey?: TKey; // placeholder for free-source rows
}

/* The functions that can run on their own model. Order = display order. */
export const TASKS: ReadonlyArray<TaskModel> = [
  { key: "vision", labelKey: "aux-vision", descKey: "aux-vision-desc", field: "vision_model", source: "catalog" },
  { key: "card", labelKey: "aux-card", descKey: "aux-card-desc", field: "card_model", source: "catalog" },
  { key: "imageprompt", labelKey: "aux-imgprompt", descKey: "aux-imgprompt-desc", field: "image_prompt_model", source: "catalog" },
  { key: "imagegen", labelKey: "aux-imagegen", descKey: "aux-imagegen-desc", field: "image_model", source: "free", phKey: "image-gen-ph" },
];

export function TaskModels({
  values,
  catalog,
  onApply,
}: {
  values: Record<string, string | undefined>;
  catalog: SelectOption[];
  onApply: (field: string, value: string) => void;
}) {
  const t = useT();
  return (
    <div className="aux-sec">
      <h3 className="aux-title">{t("aux-title")}</h3>
      <div className="aux-subline">{t("aux-sub")}</div>
      {TASKS.map((task) => (
        <TaskModelRow
          key={task.key}
          task={task}
          value={values[task.field] || ""}
          options={task.source === "catalog" ? catalog : []}
          onApply={(v) => onApply(task.field, v)}
        />
      ))}
    </div>
  );
}

function TaskModelRow({
  task,
  value,
  options,
  onApply,
}: {
  task: TaskModel;
  value: string;
  options: SelectOption[];
  onApply: (v: string) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  return (
    <div className="aux-row">
      <div className="aux-main">
        <div className="aux-head">
          <b>{t(task.labelKey)}</b>
          <span className="aux-desc">{t(task.descKey)}</span>
        </div>
        <div className="aux-cur">{value ? <code>{value}</code> : t("aux-auto")}</div>
      </div>
      {!editing ? (
        <div className="aux-acts">
          {value && <button className="btn text sm" onClick={() => onApply("")}>{t("aux-use-main")}</button>}
          <button className="btn text sm" onClick={() => { setDraft(value); setEditing(true); }}>{t("aux-change")}</button>
        </div>
      ) : (
        <div className="aux-edit">
          <Select
            value={draft}
            options={options}
            onChange={setDraft}
            search={task.source === "catalog"}
            allowCustom
            placeholder={task.phKey ? t(task.phKey) : t("model-other-ph")}
          />
          <div className="acts">
            <button className="btn primary sm" onClick={() => { onApply(draft.trim()); setEditing(false); }}>{t("aux-apply")}</button>
            <button className="btn text sm" onClick={() => setEditing(false)}>{t("cancel")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
