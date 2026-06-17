import { useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";

/* R10 — the GLOBAL image-generation key + model (Volcano Ark / Seedream), ported
   from the deleted front/web/app.js. The character's generate_image tool and the
   card/visuals pipeline both use it. The key is write-only: the server returns
   only has_image_key (presence), never the secret. */

interface ImageDefaults {
  has_image_key?: boolean;
  image_model?: string;
}

export function ImageKeyPane() {
  const t = useT();
  const { hub } = useHubApi();
  const [has, setHas] = useState(false);
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let on = true;
    hub
      .call<ImageDefaults>("defaults.get", {}, 15000)
      .then((d) => {
        if (!on) return;
        setHas(Boolean(d?.has_image_key));
        setModel(String(d?.image_model || ""));
      })
      .catch(() => {});
    return () => {
      on = false;
    };
  }, [hub]);

  const save = async () => {
    setSaving(true);
    try {
      const payload: Record<string, string> = { image_model: model.trim() };
      if (key.trim()) payload.image_api_key = key.trim(); // only send the key when (re)entered
      const d = await hub.call<ImageDefaults>("defaults.set", payload, 15000);
      setHas(Boolean(d?.has_image_key));
      setModel(String(d?.image_model || model));
      setKey(""); // never keep the secret in the field
      deckToast(t("saved"));
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="image-key-block">
      <div className="set-row">
        <div className="lbl">
          <span>{t("set-image")}</span>
          <small>{t("image-sub")}</small>
        </div>
      </div>
      <div className="set-row">
        <div className="lbl">
          <span>{t("image-key-label")}</span>
        </div>
        <input
          className="searchfield"
          type="password"
          placeholder={has ? "••••••••  (saved)" : t("image-key-ph")}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </div>
      <div className="set-row">
        <div className="lbl">
          <span>{t("image-model-label")}</span>
        </div>
        <input
          className="searchfield"
          placeholder="doubao-seedream-5-0-260128"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
      </div>
      <div className="set-row">
        <div className="grow" />
        <button className="btn primary" disabled={saving} onClick={() => void save()}>
          {saving ? t("saving") : t("keys-add")}
        </button>
      </div>
    </div>
  );
}
